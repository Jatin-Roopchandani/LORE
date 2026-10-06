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

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.registry import list_books
from lore.inference.gemini import (
    GeminiAuthError, GeminiError, GeminiKeyFileError, GeminiQuotaError,
    default_key_file, resolve_api_key,
)
from lore.mcp.guards import TIMEOUT_MS, CypherRejected, run_guarded
from lore.mcp.shapes import (
    BookOut, ChunkHit, CypherResult, EdgeOut, EntityCard, IngestStatus,
    JobOut, NodeOut, SubgraphOut, error, to_search_result,
)
from lore.mcp.stores import StoresMissing, open_stores
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
    with open_stores(config) as stores:
        books = list_books(stores.conn)
        # One query, not one per book: the entity count per book id. Verified
        # against Kuzu: UNWIND skips entities whose `books` is NULL, and a
        # book with no entities simply has no row, hence the .get(..., 0).
        counts: dict[str, int] = {}
        for row in stores.kuzu.execute(
            "MATCH (e:Entity) UNWIND e.books AS book RETURN book, count(e)"
        ):
            counts[row[0]] = int(row[1])
        return {"books": [
            BookOut(id=b["id"], title=b["title"], author=b["author"],
                    status=b["status"], chunk_count=b["chunk_count"],
                    entity_count=counts.get(b["id"], 0)).model_dump()
            for b in books
        ]}


@_guarded
def ingest_status(config: LoreConfig, book_id: str) -> dict:
    with open_stores(config) as stores:
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
SUBGRAPH_EDGE_CAP = 500

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


def _entity_card(kuzu, entity_id: str) -> EntityCard:
    """The entity row plus every REL edge in and out, in a stable order.

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
    edges_out = [
        _edge(row[1], row[0], r[7], r[8], r) for r in kuzu.execute(
            f"MATCH (e:Entity)-[r:REL]->(b:Entity) WHERE e.id = $id"
            f" RETURN {_EDGE_COLUMNS}, b.name, b.id"
            f" ORDER BY {_EDGE_ORDER}, b.id",
            {"id": entity_id},
        )
    ]
    edges_in = [
        _edge(r[7], r[8], row[1], row[0], r) for r in kuzu.execute(
            f"MATCH (b:Entity)-[r:REL]->(e:Entity) WHERE e.id = $id"
            f" RETURN {_EDGE_COLUMNS}, b.name, b.id"
            f" ORDER BY {_EDGE_ORDER}, b.id",
            {"id": entity_id},
        )
    ]
    return EntityCard(id=row[0], name=row[1], kind=row[2] or "",
                      aliases=list(row[3] or []), books=list(row[4] or []),
                      edges_out=edges_out, edges_in=edges_in)


@_guarded
def entity(config: LoreConfig, name: str) -> dict:
    with open_stores(config) as stores:
        try:
            entity_id = _resolve_one(stores.kuzu, name)
            return _entity_card(stores.kuzu, entity_id).model_dump()
        except _NotFound:
            return error("not_found", f"no entity named {name!r}")
        except _Ambiguous as exc:
            return _ambiguous_error(name, exc)


def _expand(kuzu, root: str, depth: int, node_cap: int,
            edge_cap: int = SUBGRAPH_EDGE_CAP) -> tuple[list[NodeOut], list[EdgeOut], bool]:
    """Breadth-first from `root`: up to `depth` hops, `node_cap` nodes.

    Returns (nodes, edges, truncated). Nodes are in discovery order (root
    first); edges are every REL edge between two collected nodes, so the
    result is drawable with no dangling endpoint.

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
    # LIMIT cap+1 over a TOTAL order: the extra row only detects overflow, and
    # which edges survive is the same on every run. Both ends are collected
    # nodes, so a kept edge never dangles.
    names = {i: info[i][1] for i in info}
    rows = list(kuzu.execute(
        f"MATCH (s:Entity)-[r:REL]->(o:Entity)"
        f" WHERE s.id IN $ids AND o.id IN $ids"
        f" RETURN {_EDGE_COLUMNS}, s.id, o.id"
        f" ORDER BY s.id, o.id, {_EDGE_ORDER}"
        f" LIMIT {int(edge_cap) + 1}",
        {"ids": order},
    ))
    if len(rows) > edge_cap:
        truncated = True
        rows = rows[:edge_cap]
    edges = [_edge(names[r[7]], r[7], names[r[8]], r[8], r)
             for r in rows if r[7] in names and r[8] in names]
    return nodes, edges, truncated


@_guarded
def subgraph(config: LoreConfig, entity_name: str, depth: int = 1) -> dict:
    # The parameter is `entity_name`: `entity` is a function in this module,
    # and shadowing it here is how the shared resolver stops being called.
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


# --- vector_search and cypher_query ---------------------------------------------

# A client may ask for any `limit`; it is clamped to this range so nobody can
# request a million passages. The result does not say it was clamped, because
# the count returned is visible and every chunk is full text.
VECTOR_LIMIT_MIN = 1
VECTOR_LIMIT_MAX = 50


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
    with open_stores(config) as stores:
        titles = {b["id"]: b["title"] for b in list_books(stores.conn)}
        # Checked before searching: a filter that matches nothing returns an
        # empty list, which looks the same as "no passages matched".
        if book_id is not None and book_id not in titles:
            return error("not_found", f"no book with id {book_id!r}")
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
        finally:
            if embedder is not None:
                await embedder.aclose()
        if book_id is None:
            rows = stores.lance.search_chunks(vector, limit=limit)
        else:
            rows = stores.lance.search_chunks_in_book(vector, book_id,
                                                      limit=limit)
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
                      models: QueryModels | None = None) -> dict:
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
                                           stores.lance, query, models)
            return to_search_result(answer, titles).model_dump()
        except GeminiError as exc:
            return _upstream(exc)
        finally:
            if models is not None:
                await models.aclose()
