"""The MCP tools, as plain functions of a LoreConfig.

Plain so they are testable without a server; a later task registers them.
No tool raises through the protocol: every outcome is a dict a client can
read, either the tool's shape or an `error(...)` envelope.
"""
from __future__ import annotations

import datetime
import functools
import inspect
import math

import httpx

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import list_books
from lore.inference.gemini import (
    GeminiAuthError, GeminiError, GeminiKeyFileError, GeminiQuotaError,
    default_key_file, resolve_api_key,
)
from lore.mcp.guards import TIMEOUT_MS, CypherRejected, run_guarded
from lore.mcp.shapes import (
    BookOut, ChunkHit, Citation, CypherResult, EdgeOut, EntityCard,
    IngestStatus, JobOut, LinkOut, NodeOut, SubgraphOut, error,
    to_search_result,
)
from lore.mcp.stores import StoresMissing, open_graph, open_stores
from lore.orchestrator.gemini_budget import (
    BudgetExceeded, check_budget, recorder_for, today,
)
from lore.query.agent import QueryModels, answer_question, make_query_models
from lore.query.seed import (
    canonical_ids, embed_question, live_entity_ids, load_name_index,
    named_entity_ids,
)


def _guarded(fn):
    """Turn StoresMissing into the no_graph envelope, for every tool.

    One decorator, so no tool's handling can drift from the others'.
    StoresUnavailable subclasses StoresMissing, so a graph that exists but
    is locked or corrupt lands here too, carrying its own honest message.

    Only anticipated, spec-coded conditions are handled. Unexpected
    exceptions are intentionally left to the MCP SDK, which returns
    isError with a generic message and logs the traceback -- do not add a
    catch-all here.
    """
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except StoresMissing as exc:
                return error("no_graph", str(exc))
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoresMissing as exc:
            return error("no_graph", str(exc))
    return wrapper


@_guarded
def library(config: LoreConfig) -> dict:
    """Every book, from SQLite; entity counts from the graph if it opens.

    The graph is best-effort: between `lore ingest` and `lore graph load`,
    or while a load holds the lock, the books are still listed, with
    `entity_count` None (unknown, not zero) and a `note` saying why.
    """
    with open_stores(config, kuzu=False, lance=False) as stores:
        books = list_books(stores.conn)
    counts: dict[str, int] | None = {}
    note = None
    try:
        with open_graph(config) as kuzu:
            # One query, not one per book: the entity count per book id.
            # Verified against Kuzu: UNWIND skips entities whose `books` is
            # NULL, and a book with no entities simply has no row, hence
            # the .get(..., 0).
            for row in kuzu.execute(
                "MATCH (e:Entity) UNWIND e.books AS book RETURN book, count(e)"
            ):
                counts[row[0]] = int(row[1])
    except StoresMissing as exc:  # StoresUnavailable included
        counts = None
        note = f"the graph is unavailable, so entity counts are null: {exc}"
    return {"books": [
        BookOut(id=b["id"], title=b["title"], author=b["author"],
                status=b["status"], chunk_count=b["chunk_count"],
                entity_count=None if counts is None else counts.get(b["id"], 0),
                ).model_dump()
        for b in books
    ], "note": note}


@_guarded
def ingest_status(config: LoreConfig, book_id: str) -> dict:
    # SQLite only: this tool watches the pipeline, so it must answer while
    # the graph is absent or locked by a load.
    with open_stores(config, kuzu=False, lance=False) as stores:
        books = {b["id"]: b for b in list_books(stores.conn)}
        if book_id not in books:
            return error("not_found", f"no book with id {book_id!r}")
        jobs = [
            JobOut(kind=r[0], state=r[1], detail=r[2], error=r[3],
                   updated_at=r[4])
            for r in stores.conn.execute(
                "SELECT kind, state, detail, error, updated_at FROM jobs"
                " WHERE book_id = ? ORDER BY updated_at, id",
                (book_id,),
            )
        ]
        # A dead letter is a job in state "failed": orchestrator/run.py only
        # ever sets queued, dispatched, completed, failed -- there is no
        # "dead" state, so filtering on it would always be empty and hide
        # every failed extraction. Do not "correct" this to "dead".
        dead = [j for j in jobs if j.state == "failed"]
        return IngestStatus(book_id=book_id, title=books[book_id]["title"],
                            status=books[book_id]["status"], jobs=jobs,
                            dead_letters=dead).model_dump()


# --- the graph tools -----------------------------------------------------------

MAX_DEPTH = 3
SUBGRAPH_NODE_CAP = 300
# Nodes alone do not bound edges: Holmes at depth 3 was 286 nodes, 995 edges,
# ~344 KB. Evidence text is never shortened to fit; edges are dropped instead.
# Links (one per subject->object pair), not raw edges, kept strongest first.
# Measured on the live graph with one citation each: Holmes at depth 1 has
# 180 pairs (83 KB, refused by Claude Code); 100 keeps his real relationships
# and fits one tool result.
SUBGRAPH_EDGE_CAP = 100
CITATIONS_PER_LINK = 1
# A safety bound on raw REL rows read to build the links. Far above this
# corpus (1,046 edges in total); if it is ever hit, `truncated` is set.
SUBGRAPH_RAW_EDGE_LIMIT = 20_000
# Per direction, on the entity card. 50 + 50 Holmes edges is ~36 KB, well
# inside one tool result; the uncapped 283 was 103 KB and was refused.
CARD_EDGE_CAP = 50

# Every Kuzu query below that is capped or whose rows are presented ends in a
# TOTAL ORDER BY (down to unique fields), and none uses RETURN DISTINCT with
# ORDER BY (Kuzu 0.11.3 returns that out of order about one run in four; see
# lore/query/templates.py). Phase 3 spent a whole task adding exactly this to
# the query agent: an unordered query plus a cap returns a different subset
# each run, and a UI would redraw a different graph with nothing to blame.
# Do not "simplify" an ORDER BY away, and never build output order from a set.

_EDGE_COLUMNS = (
    "r.predicate, r.predicate_raw, r.evidence, r.confidence, r.book_id,"
    " r.chapter_seq, r.chunk_id"
)
# Every EdgeOut field, so two rows tie only when they would print identically.
_EDGE_ORDER = (
    "r.book_id, r.chapter_seq, r.chunk_id, r.predicate, r.predicate_raw,"
    " r.evidence, r.confidence"
)


class _NotFound(Exception):
    pass


class _Ambiguous(Exception):
    def __init__(self, candidates: list[tuple[str, str]]) -> None:
        # [(id, name), ...]
        self.candidates = candidates


def _resolve_one(kuzu, name: str) -> str:
    """One canonical entity id for `name`, or raise.

    Resolves through NameIndex and canonical_ids -- the SAME path the query
    agent uses -- so an alias finds its canonical identically. Do not write
    a second name-matching rule here; two rules that drift is how a tool
    and an answer start disagreeing about who someone is.

    Edgeless entities: the agent drops them as seeds (they can spend a slot
    and never return a row), and so do we WHEN a live candidate exists. If
    every candidate is edgeless we keep them: the entity is real, "no entity
    named X" would be a lie, and its card is honestly empty.
    """
    index = load_name_index(kuzu)
    ids = named_entity_ids(name, index)
    if not ids:
        raise _NotFound()
    canonical = canonical_ids(kuzu, ids)
    distinct = list(dict.fromkeys(live_entity_ids(kuzu, canonical) or canonical))
    if len(distinct) > 1:
        rows = {
            row[0]: row[1]
            for row in kuzu.execute(
                "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name"
                " ORDER BY e.id",
                {"ids": distinct},
            )
        }
        raise _Ambiguous([(i, rows.get(i, i)) for i in sorted(distinct)])
    return distinct[0]


def _ambiguous_error(name: str, exc: _Ambiguous) -> dict:
    listed = ", ".join(f"{n} ({i})" for i, n in exc.candidates)
    return error(
        "not_found",
        f"{name!r} is ambiguous -- it matches {len(exc.candidates)}"
        f" entities: {listed}. Ask again with one of those names.",
    )


def _edge(subject: str, subject_id: str, obj: str, object_id: str,
          row) -> EdgeOut:
    # `or ""`: predicate_raw is NULL on edges written before that column was
    # added (kuzu_store migrates it in as NULL), and EdgeOut wants a str.
    return EdgeOut(
        subject=subject, predicate=row[0] or "", predicate_raw=row[1] or "",
        object=obj, subject_id=subject_id, object_id=object_id,
        evidence=row[2] or "", confidence=float(row[3] or 0.0),
        book_id=row[4] or "", chapter_seq=int(row[5] or 0),
        chunk_id=row[6] or "",
    )


def _entity_card(kuzu, entity_id: str, edge_cap: int) -> EntityCard:
    """The entity row plus up to `edge_cap` REL edges each way, in a stable
    order, with the true totals.

    Capped because the uncapped card for the live graph's biggest hub (Mr.
    Sherlock Holmes: 172 out, 111 in) was a 103 KB result that Claude Code
    refused as a single tool response. The cap sits on the same total
    ORDER BY as before, so which edges survive is deterministic.

    Edge subject/object are names and subject_id/object_id the ids, as in
    the subgraph: one meaning for EdgeOut in every tool.
    """
    rows = list(kuzu.execute(
        "MATCH (e:Entity) WHERE e.id = $id"
        " RETURN e.id, e.name, e.kind, e.aliases, e.books",
        {"id": entity_id},
    ))
    if not rows:  # resolved a moment ago; only a concurrent rebuild gets here
        raise _NotFound()
    row = rows[0]
    # b.id after the edge columns: two edges to different entities with the
    # same name and text must not tie.
    params = {"id": entity_id, "cap": edge_cap}
    edges_out = [
        _edge(row[1], row[0], r[7], r[8], r) for r in kuzu.execute(
            f"MATCH (e:Entity)-[r:REL]->(b:Entity) WHERE e.id = $id"
            f" RETURN {_EDGE_COLUMNS}, b.name, b.id"
            f" ORDER BY {_EDGE_ORDER}, b.id LIMIT $cap",
            params,
        )
    ]
    edges_in = [
        _edge(r[7], r[8], row[1], row[0], r) for r in kuzu.execute(
            f"MATCH (b:Entity)-[r:REL]->(e:Entity) WHERE e.id = $id"
            f" RETURN {_EDGE_COLUMNS}, b.name, b.id"
            f" ORDER BY {_EDGE_ORDER}, b.id LIMIT $cap",
            params,
        )
    ]
    out_total = _count(kuzu, "MATCH (e:Entity)-[r:REL]->(:Entity)"
                             " WHERE e.id = $id RETURN count(r)", entity_id)
    in_total = _count(kuzu, "MATCH (:Entity)-[r:REL]->(e:Entity)"
                            " WHERE e.id = $id RETURN count(r)", entity_id)
    return EntityCard(id=row[0], name=row[1], kind=row[2] or "",
                      aliases=list(row[3] or []), books=list(row[4] or []),
                      edges_out=edges_out, edges_in=edges_in,
                      edges_out_total=out_total, edges_in_total=in_total,
                      truncated=(len(edges_out) < out_total
                                 or len(edges_in) < in_total))


def _count(kuzu, cypher: str, entity_id: str) -> int:
    rows = list(kuzu.execute(cypher, {"id": entity_id}))
    return int(rows[0][0]) if rows else 0


@_guarded
def entity(config: LoreConfig, name: str) -> dict:
    with open_stores(config) as stores:
        try:
            entity_id = _resolve_one(stores.kuzu, name)
            # Read at call time, so a test can monkeypatch the module value.
            return _entity_card(stores.kuzu, entity_id,
                                CARD_EDGE_CAP).model_dump()
        except _NotFound:
            return error("not_found", f"no entity named {name!r}")
        except _Ambiguous as exc:
            return _ambiguous_error(name, exc)


def _expand(kuzu, root: str, depth: int, node_cap: int,
            edge_cap: int = SUBGRAPH_EDGE_CAP) -> tuple[list[NodeOut], list[LinkOut], bool]:
    """Breadth-first from `root`: up to `depth` hops, `node_cap` nodes.

    Returns (nodes, links, truncated). Nodes are in discovery order (root
    first); links group every REL edge between two collected nodes by
    (subject, object), so the result is drawable with no dangling endpoint
    and no pair drawn thirty times.

    Deterministic by construction, not by luck: the frontier is a list kept
    in discovery order, each hop's neighbour query is ordered by (frontier
    id, neighbour id), so which nodes survive the cap never depends on
    Kuzu's scan order or on set iteration. `seen` is a set used ONLY for
    membership, never iterated into output. It also makes cycles, and a
    walk that returns to the root, terminate.
    """
    order = [root]
    seen = {root}
    frontier = [root]
    truncated = False
    for _ in range(depth):
        if not frontier or truncated:
            break
        found: list[str] = []
        # No DISTINCT: it does not mix reliably with ORDER BY in this Kuzu.
        # Parallel edges give repeat rows, dropped by `seen` below.
        for _a, b in kuzu.execute(
            "MATCH (a:Entity)-[r:REL]-(b:Entity) WHERE a.id IN $frontier"
            " RETURN a.id, b.id ORDER BY a.id, b.id",
            {"frontier": frontier},
        ):
            if b in seen:
                continue
            if len(order) >= node_cap:
                truncated = True
                break
            seen.add(b)
            order.append(b)
            found.append(b)
        frontier = found
    info = {
        row[0]: row for row in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids"
            " RETURN e.id, e.name, e.kind, e.books ORDER BY e.id",
            {"ids": order},
        )
    }
    nodes = [
        NodeOut(id=i, name=info[i][1], kind=info[i][2] or "",
                books=list(info[i][3] or []))
        for i in order if i in info
    ]
    names = {i: info[i][1] for i in info}
    links, cut = _links_among(kuzu, order, names, edge_cap)
    truncated = truncated or cut
    # Keep the root and every node a kept link touches. Uncapped, this drops
    # nothing (each discovered node has the link it was discovered by); capped,
    # it drops nodes reachable only through cut links. Live graph: Holmes at
    # depth 3 kept 100 links but listed 286 nodes (75 KB) before this.
    touched = {root} | {l.subject_id for l in links} | {l.object_id for l in links}
    nodes = [n for n in nodes if n.id in touched]
    return nodes, links, truncated


def _links_among(kuzu, ids: list[str], names: dict[str, str],
                 edge_cap: int) -> tuple[list[LinkOut], bool]:
    """Every REL edge between two of `ids`, one LinkOut per directed pair,
    strongest first, at most `edge_cap`. Returns (links, truncated)."""
    truncated = False
    # Raw rows over a TOTAL order, so grouping is deterministic: within a
    # pair, edges arrive in book/chapter/chunk order and the first are the
    # citations. Both ends are collected nodes, so no link dangles.
    rows = list(kuzu.execute(
        f"MATCH (s:Entity)-[r:REL]->(o:Entity)"
        f" WHERE s.id IN $ids AND o.id IN $ids"
        f" RETURN {_EDGE_COLUMNS}, s.id, o.id"
        f" ORDER BY s.id, o.id, {_EDGE_ORDER}"
        f" LIMIT {SUBGRAPH_RAW_EDGE_LIMIT + 1}",
        {"ids": ids},
    ))
    if len(rows) > SUBGRAPH_RAW_EDGE_LIMIT:
        truncated = True
        rows = rows[:SUBGRAPH_RAW_EDGE_LIMIT]
    groups: dict[tuple[str, str], list] = {}
    for r in rows:
        if r[7] in names and r[8] in names:
            groups.setdefault((r[7], r[8]), []).append(r)
    # Strongest first, then (subject id, object id): a total order, so a
    # capped hub keeps the same links on every run -- its real relationships,
    # not whichever ids sort lowest.
    ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if len(ranked) > edge_cap:
        truncated = True
        ranked = ranked[:edge_cap]
    links = [_link(names[s], s, names[o], o, group) for (s, o), group in ranked]
    return links, truncated


def _link(subject: str, subject_id: str, obj: str, object_id: str,
          group: list) -> LinkOut:
    counts: dict[str, int] = {}
    for r in group:
        counts[r[0] or ""] = counts.get(r[0] or "", 0) + 1
    # Most frequent predicate first, then by name: dict order is part of the
    # JSON, and the output must be byte-identical across runs.
    predicates = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    citations = [
        Citation(predicate=r[0] or "", predicate_raw=r[1] or "",
                 evidence=r[2] or "", confidence=float(r[3] or 0.0),
                 book_id=r[4] or "", chapter_seq=int(r[5] or 0),
                 chunk_id=r[6] or "")
        for r in group[:CITATIONS_PER_LINK]
    ]
    return LinkOut(subject=subject, subject_id=subject_id, object=obj,
                   object_id=object_id, count=len(group),
                   predicates=predicates, citations=citations)


@_guarded
def subgraph(config: LoreConfig, entity_name: str, depth: int = 1) -> dict:
    with open_stores(config) as stores:
        try:
            root = _resolve_one(stores.kuzu, entity_name)
        except _NotFound:
            return error("not_found", f"no entity named {entity_name!r}")
        except _Ambiguous as exc:
            return _ambiguous_error(entity_name, exc)

        clamped = max(1, min(depth, MAX_DEPTH))
        note = None
        if clamped != depth:
            note = (f"depth {depth} was capped to {clamped} (allowed range"
                    f" 1-{MAX_DEPTH}): a hub entity at greater depth walks"
                    f" most of the graph")

        nodes, edges, truncated = _expand(stores.kuzu, root, clamped,
                                          SUBGRAPH_NODE_CAP,
                                          SUBGRAPH_EDGE_CAP)
        return SubgraphOut(nodes=nodes, edges=edges, truncated=truncated,
                           depth=clamped, note=note).model_dump()


def _exists(kuzu, entity_id: str) -> bool:
    return bool(list(kuzu.execute(
        "MATCH (e:Entity) WHERE e.id = $id RETURN e.id", {"id": entity_id})))


@_guarded
def entity_by_id(config: LoreConfig, entity_id: str) -> dict:
    """`entity`, for a caller that already holds the id (the web UI, from
    a search's edges). No name resolution, so no ambiguity."""
    with open_stores(config) as stores:
        if not _exists(stores.kuzu, entity_id):
            return error("not_found", f"no entity with id {entity_id!r}")
        canonical = canonical_ids(stores.kuzu, [entity_id])[0]
        return _entity_card(stores.kuzu, canonical, CARD_EDGE_CAP).model_dump()


# The web UI asks for the kinds of every node a search drew; more than a
# few hundred in one request is not a graph anyone reads.
ENTITIES_BY_IDS_CAP = 300


@_guarded
def entities_by_ids(config: LoreConfig, ids: list[str]) -> dict:
    """{id, name, kind, books} for each known id, read-only; unknown ids are
    left out. At most ENTITIES_BY_IDS_CAP distinct ids are looked up."""
    wanted = list(dict.fromkeys(i for i in ids if i))[:ENTITIES_BY_IDS_CAP]
    if not wanted:
        return {"entities": []}
    with open_stores(config) as stores:
        rows = stores.kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids"
            " RETURN e.id, e.name, e.kind, e.books ORDER BY e.id",
            {"ids": wanted})
        return {"entities": [
            {"id": r[0], "name": r[1] or "", "kind": r[2] or "",
             "books": list(r[3] or [])} for r in rows]}


@_guarded
def subgraph_by_id(config: LoreConfig, entity_id: str, depth: int = 1) -> dict:
    with open_stores(config) as stores:
        if not _exists(stores.kuzu, entity_id):
            return error("not_found", f"no entity with id {entity_id!r}")
        root = canonical_ids(stores.kuzu, [entity_id])[0]
        clamped = max(1, min(depth, MAX_DEPTH))
        nodes, edges, truncated = _expand(stores.kuzu, root, clamped,
                                          SUBGRAPH_NODE_CAP, SUBGRAPH_EDGE_CAP)
        return SubgraphOut(nodes=nodes, edges=edges, truncated=truncated,
                           depth=clamped, note=None).model_dump()


# The library overview: what the web UI draws before any question.
OVERVIEW_LIMIT_MIN = 1
OVERVIEW_LIMIT_MAX = 100
# 150 drew a hairball at 40 nodes; 60 keeps the strongest relationships legible.
OVERVIEW_LINK_CAP = 60


@_guarded
def overview(config: LoreConfig, limit: int = 40) -> dict:
    """The `limit` most-connected canonical entities and the links among
    them only, read-only, SubgraphOut-shaped (depth 0).

    Degree is REL edges in both directions. Alias stubs (an outgoing
    ALIAS_OF) and edgeless entities are left out. Nodes are ordered by
    degree, then id; links strongest first, at most OVERVIEW_LINK_CAP.
    `truncated` says entities or links were left out.
    """
    clamped = max(OVERVIEW_LIMIT_MIN, min(limit, OVERVIEW_LIMIT_MAX))
    with open_stores(config) as stores:
        kuzu = stores.kuzu
        degree: dict[str, int] = {}
        # Counted per direction, so a self-loop counts on both ends, as on
        # the entity card. Ranking happens in Python over a total order.
        for cypher in ("MATCH (e:Entity)-[r:REL]->(:Entity)"
                       " RETURN e.id, count(r) ORDER BY e.id",
                       "MATCH (:Entity)-[r:REL]->(e:Entity)"
                       " RETURN e.id, count(r) ORDER BY e.id"):
            for entity_id, n in kuzu.execute(cypher):
                degree[entity_id] = degree.get(entity_id, 0) + int(n)
        stubs = {row[0] for row in kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(:Entity) RETURN a.id ORDER BY a.id")}
        ranked = sorted(((-d, i) for i, d in degree.items()
                         if d > 0 and i not in stubs))
        truncated = len(ranked) > clamped
        top = [i for _d, i in ranked[:clamped]]
        info = {
            row[0]: row for row in kuzu.execute(
                "MATCH (e:Entity) WHERE e.id IN $ids"
                " RETURN e.id, e.name, e.kind, e.books ORDER BY e.id",
                {"ids": top},
            )
        }
        nodes = [
            NodeOut(id=i, name=info[i][1] or "", kind=info[i][2] or "",
                    books=list(info[i][3] or []))
            for i in top if i in info
        ]
        names = {i: info[i][1] or "" for i in info}
        links, cut = _links_among(kuzu, top, names, OVERVIEW_LINK_CAP)
        return SubgraphOut(nodes=nodes, edges=links, truncated=truncated or cut,
                           depth=0, note=None).model_dump()


# --- vector_search and cypher_query ---------------------------------------------

# A client may ask for any `limit`; it is clamped to this range so nobody can
# request a million passages. The result does not say it was clamped, because
# the count returned is visible and every chunk is full text.
VECTOR_LIMIT_MIN = 1
VECTOR_LIMIT_MAX = 50


def _ollama_unavailable(config: LoreConfig, exc: httpx.HTTPError) -> dict:
    """The local model server failed: refused, timed out, or answered an
    HTTP error (a model not pulled is a 404).

    Caught, not left to the SDK: mcp 2.3.0 reports an escaped exception as
    the bare "Error executing tool ...", and on this machine Ollama is
    normally stopped and started by hand, so this is the likeliest failure
    of the tools documented as always working. The start command is the
    CPU-only one on purpose: loading a model onto this machine's GPU
    reboots the whole WSL VM.
    """
    return error(
        "upstream_unavailable",
        f"Ollama at {config.ollama.host} failed: {exc}. vector_search and"
        " lore_search embed the question with the local Ollama model, so"
        " Ollama must be running; start it (CPU-only) with"
        " `CUDA_VISIBLE_DEVICES=-1 ~/.local/ollama/bin/ollama serve`.",
    )


def _finite_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@_guarded
async def vector_search(config: LoreConfig, query: str,
                        book_id: str | None = None, limit: int = 10,
                        embed=None) -> dict:
    """Raw passages nearest `query` by meaning; `limit` is clamped to 1-50.

    Embeds locally, so it spends no Gemini budget and keeps working after
    the daily cap. `embed` is for tests; None builds the local embedder.
    """
    limit = max(VECTOR_LIMIT_MIN, min(int(limit), VECTOR_LIMIT_MAX))
    # Three short phases, and no store is held across the embedding, which
    # can take seconds (or wait on a stopped Ollama). Kuzu is never opened:
    # this tool needs only SQLite and LanceDB, so it keeps working while the
    # graph is absent or a rebuild holds its lock.
    #
    # 1. Check the library exists and the book is real. Checked before
    #    searching: a filter that matches nothing returns an empty list,
    #    which looks the same as "no passages matched".
    with open_stores(config, kuzu=False) as stores:
        titles = {b["id"]: b["title"] for b in list_books(stores.conn)}
    if book_id is not None and book_id not in titles:
        return error("not_found", f"no book with id {book_id!r}")

    # 2. Embed, holding nothing.
    embedder = None
    if embed is None:
        # Must NOT use make_query_models: it builds the Gemini-gated
        # stack and raises "needs an API key" when selection/synthesis
        # are on Gemini, which would break the one tool documented as
        # needing no key. Only the local embedder is built, and closed.
        from lore.query.agent import make_query_embedder
        embedder = make_query_embedder(config)
        embed = embedder.embed
    try:
        # QUERY_PREFIX is applied exactly once, inside embed_question.
        vector = await embed_question(query, embed)
    except httpx.HTTPError as exc:
        return _ollama_unavailable(config, exc)
    finally:
        if embedder is not None:
            await embedder.aclose()

    # 3. Search.
    with open_stores(config, kuzu=False) as stores:
        if book_id is None:
            rows = stores.lance.search_chunks(vector, limit=limit)
        else:
            rows = stores.lance.search_chunks_in_book(vector, book_id,
                                                      limit=limit)
            # Chunks are embedded only by `lore graph load`. A book that was
            # chunked but never loaded (the live "Pride and Prejudice": 731
            # chunks, 0 vectors) would otherwise come back [] and read as
            # "nothing matched". A book WITH rows and no match stays [].
            if not rows and not stores.lance.book_has_chunks(book_id):
                return error(
                    "not_found",
                    f"{titles[book_id]!r} has no embedded passages yet --"
                    " passages are embedded by `lore graph load`",
                )
        # `_distance` missing or non-finite is None, never seed.py's inf.
        return {"chunks": [
            ChunkHit(chunk_id=r["chunk_id"], book_id=r["book_id"],
                     book_title=titles.get(r["book_id"], r["book_id"]),
                     chapter_seq=int(r["chapter_seq"]), text=r["text"],
                     distance=_finite_or_none(r.get("_distance"))
                     ).model_dump()
            for r in rows
        ]}


def _plain(value):
    """Kuzu's value as plain JSON: dicts and lists recurse, dates are ISO
    strings, non-finite floats are None, anything else not JSON-native is
    str(...). Kuzu returns nodes and rels as dicts (with internal `_id`
    sub-dicts), so this is not optional."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


# Kuzu's real timeout error, measured on 0.11.3 with a 200 ms limit on an
# ORDER BY over a three-way cross product: RuntimeError('Interrupted.').
_KUZU_TIMEOUT_MESSAGE = "Interrupted."


@_guarded
def cypher_query(config: LoreConfig, query: str) -> dict:
    """Run one read-only Cypher statement.

    run_guarded validates BEFORE Kuzu sees the string. That ordering is the
    whole control: Kuzu on a read-only connection will happily execute
    `COPY (...) TO '<path>'` and write the file. Never call kuzu.execute
    directly here.
    """
    with open_stores(config) as stores:
        try:
            columns, rows, truncated = run_guarded(stores.kuzu, query)
        except CypherRejected as exc:
            return error("cypher_rejected", exc.reason)
        except RuntimeError as exc:
            # The exact timeout text first; anything else Kuzu raised here is
            # the engine refusing the query (a parser or binder exception --
            # the usual outcome of LLM-written Cypher). It is returned, not
            # re-raised: measured on mcp 2.3.0, an escaped exception reaches
            # the client only as "Error executing tool cypher_query", with
            # Kuzu's message dropped, so the caller cannot see its typo.
            if str(exc) == _KUZU_TIMEOUT_MESSAGE:
                return error("timeout", f"the query exceeded {TIMEOUT_MS}ms")
            return error("cypher_rejected", f"Kuzu could not run the query: {exc}")
        return CypherResult(
            columns=columns, rows=[[_plain(v) for v in row] for row in rows],
            truncated=truncated,
        ).model_dump()


# --- lore_search ------------------------------------------------------------------

_KEY_HELP = ("set GEMINI_API_KEY (or GOOGLE_API_KEY), or put one line"
             " `GEMINI_API_KEY=...` in ~/.config/lore/gemini.env with mode 600")


def _check_synthesis_budget(config: LoreConfig) -> str | None:
    """None if a synthesis request can proceed, else the refusal message.

    Refuses only when EVERY configured synthesis model is at its daily cap:
    the Gemini client is a cascade, so a capped first model just falls
    through to the next. Runs on an `open_db` connection, not
    `stores.conn`: a library migrated only to v3 has no `gemini_ledger`
    table, and the recorder applies the migration on this path anyway. The
    six cheap tools keep their unmigrated read-only connection.

    Only synthesis is checked: it is the step that is always spent, and the
    messages name the models it would use. Selection requests are still
    counted by the recorder.
    """
    if config.synthesis.provider != "gemini":
        return None  # the local model spends no Gemini budget
    conn = open_db(config.sqlite_path)
    try:
        refusals = []
        for model in config.synthesis.models:
            try:
                check_budget(conn, model, today())
            except BudgetExceeded as exc:
                refusals.append(str(exc))
            else:
                return None
        return " ".join(refusals) or None
    finally:
        conn.close()


class _KeyMissing(Exception):
    pass


def _build_models(config: LoreConfig) -> QueryModels:
    """The real models. The key is resolved only if a step runs on Gemini,
    so a bad key file cannot fail an all-local search. A missing key is
    checked explicitly (not by catching make_query_models' ValueError, which
    an empty model list also raises: that is a config error and propagates).
    """
    api_key = None
    if "gemini" in (config.selection.provider, config.synthesis.provider):
        api_key = resolve_api_key(key_file=default_key_file())
        if not api_key:
            raise _KeyMissing()
    return make_query_models(config, api_key=api_key,
                             recorder=recorder_for(config.sqlite_path))


def _upstream(exc: GeminiError) -> dict:
    """A Gemini failure that is not our ledger, told truthfully.

    GeminiAuthError gets fixed text rather than its own: the key is never
    in an exception message, but nothing here should depend on that.
    Never `budget_exhausted`: that sends the user to wait for midnight.
    """
    if isinstance(exc, GeminiAuthError):
        return error("upstream_unavailable",
                     "Gemini rejected the API key, so the key is missing or"
                     f" invalid. To fix it, {_KEY_HELP}.")
    if isinstance(exc, GeminiKeyFileError):
        return error("upstream_unavailable", str(exc))
    if isinstance(exc, GeminiQuotaError):
        return error("upstream_unavailable",
                     f"{exc} This is Gemini declining or cooling down, not"
                     " the daily budget LORE tracks; try again shortly.")
    return error("upstream_unavailable",
                 f"Gemini failed: {exc}. This is not the daily budget;"
                 " try again shortly.")


@_guarded
async def lore_search(config: LoreConfig, query: str,
                      models: QueryModels | None = None, on_event=None) -> dict:
    """The full query agent: spends Gemini requests, so it is guarded.

    `budget_exhausted` means the shared ledger says every synthesis model is
    spent. Every other Gemini failure is `upstream_unavailable`. `models` is
    for tests; None builds the real ones, counted in the shared ledger.
    """
    with open_stores(config) as stores:
        refusal = _check_synthesis_budget(config)
        if refusal is not None:
            if models is not None:
                await models.aclose()
            return error("budget_exhausted",
                         f"{refusal} The other LORE tools still work.")
        try:
            if models is None:
                try:
                    models = _build_models(config)
                except GeminiError as exc:
                    return _upstream(exc)
                except _KeyMissing:
                    return error("upstream_unavailable",
                                 "the Gemini API key is missing or invalid:"
                                 f" {_KEY_HELP}.")
            titles = {b["id"]: b["title"] for b in list_books(stores.conn)}
            answer = await answer_question(stores.conn, stores.kuzu,
                                           stores.lance, query, models,
                                           on_event=on_event)
            return to_search_result(answer, titles).model_dump()
        except GeminiError as exc:
            return _upstream(exc)
        except httpx.HTTPError as exc:
            # Distinct from GeminiError: GeminiClient wraps its own httpx
            # failures in GeminiTransportError, so a raw httpx error here
            # came from Ollama (the embedder, or a step set to run locally).
            return _ollama_unavailable(config, exc)
        finally:
            if models is not None:
                await models.aclose()
