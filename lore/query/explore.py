"""Read-only graph exploration for the web UI: profile, timeline, relations,
edge, search, path and passage.

Everything here reads stored data (the graph, the codex, the beats, the
chunks); nothing calls a model, except `intro`, which may make one cheap call
for the spoiler-safe introduction a profile shows under a reading position
(lore/codex/intro.py). Every function takes a `world` (id or name)
and reads only that world's books: an entity of another world is `not_found`,
exactly as in `lore.mcp.tools`. With no world and several worlds in the
library, an entity is read inside the world it lives in and a search covers
every world, each result tagged with its world.

Story order is `position.order_passages`: two books are ordered against each
other only when their series says how, and `order_note` says so when it does
not. Nothing is ordered by id, title or date.
"""
from __future__ import annotations

import difflib
import sqlite3
import time
from collections import Counter

from lore.beats.store import beats_for_entities
from lore.codex import intro as intro_module
from lore.codex.store import DISPLAY_ORDER, entries_for
from lore.config import LoreConfig
from lore.ingest.registry import book_rows
from lore.mcp.shapes import error
from lore.mcp.stores import open_stores
from lore.mcp.tools import (
    _EDGE_COLUMNS, _EDGE_ORDER, _entity_scope, _guarded, _in_world, _scoped,
    _world_error,
)
from lore.query import visible
from lore.query.models import Passage
from lore.query.position import order_passages
from lore.query.scope import UnknownWorld, world_books
from lore.query.seed import canonical_ids

EVIDENCE_PER_RELATION = 3
EVIDENCE_PER_HOP = 3
SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50
PATH_MAX_HOPS = 4
# The BFS reads one level of the graph per query; past this it stops and says so.
PATH_TIME_CAP_S = 3.0
FUZZY_MIN_RATIO = 0.78
FUZZY_MIN_CHARS = 3


# --- shared helpers -------------------------------------------------------------

def _in_story_order(rows: list[dict], books) -> tuple[list[dict], str | None]:
    """`rows` (each with chunk_id, book_id, chapter) in story order, each given
    its `book_title` and `position_label`; plus the order note, or None when
    every pair of books is ordered."""
    passages = [Passage(n=i, chunk_id=r["chunk_id"], book_id=r["book_id"],
                        chapter_seq=r["chapter"], text="")
                for i, r in enumerate(rows)]
    order = order_passages(passages, books)
    out = []
    for pos in order.ordered:
        row = rows[pos.n]
        row["book_title"] = pos.title
        row["position_label"] = pos.label()
        out.append(row)
    return out, order.note() or None


def _node(row) -> dict:
    return {"id": row[0], "name": row[1] or "", "kind": row[2] or ""}


def _entity_row(kuzu, entity_id: str):
    rows = list(kuzu.execute(
        "MATCH (e:Entity) WHERE e.id = $id RETURN e.id, e.name, e.kind, e.books",
        {"id": entity_id}))
    return rows[0]


def _flt(books) -> tuple[str, dict]:
    """The clause keeping the visible edges of `books` (a reading position may
    cap a book at a chapter) and its parameters; ("", {}) for the whole library."""
    return visible.book_clause(books), visible.book_params(books)


def _met(stores, books, ids) -> dict[str, list[str]]:
    """The names each entity is known by to the reader (earliest first) under a
    reading position; {id: []} (nothing renamed) without one."""
    return visible.visible_names(stores.kuzu, books, ids,
                                 book_rows(stores.conn) if visible.capped(books) else None)


def _shown(names: dict[str, list[str]], node: dict) -> dict:
    """`node` ({id, name, kind}) named by the earliest alias the reader met."""
    return {**node, "name": visible.display_name(names, node["id"], node["name"])}


def _edge_rows(kuzu, entity_id: str, books, other_id: str | None = None) -> list[dict]:
    """Every REL edge of the entity (both directions) within `books`, one dict
    per edge, in the graph tools' total order. `direction` is "out" when the
    entity is the subject. With `other_id`, only edges to or from that entity.
    A self-loop is left out: it relates the entity to nothing else."""
    flt, extra = _flt(books)
    out: list[dict] = []
    for direction, pattern in (("out", "(e:Entity)-[r:REL]->(b:Entity)"),
                               ("in", "(b:Entity)-[r:REL]->(e:Entity)")):
        pair = " AND b.id = $other" if other_id else ""
        params = {"id": entity_id, **extra, **({"other": other_id} if other_id else {})}
        for r in kuzu.execute(
                f"MATCH {pattern} WHERE e.id = $id AND b.id <> $id{flt}{pair}"
                f" RETURN {_EDGE_COLUMNS}, b.id, b.name, b.kind, b.books"
                f" ORDER BY {_EDGE_ORDER}, b.id", params):
            if books is not None and not any(x in books for x in r[10] or ()):
                continue  # an edge into another world is never shown
            out.append({
                "direction": direction, "predicate": r[0] or "",
                "predicate_raw": r[1] or "", "quote": r[2] or "",
                "book_id": r[4] or "", "chapter": int(r[5] or 0),
                "chunk_id": r[6] or "",
                "other": {"id": r[7], "name": r[8] or "", "kind": r[9] or ""},
            })
    return out


def _evidence(row: dict) -> dict:
    return {"quote": row["quote"], "chunk_id": row["chunk_id"],
            "book_id": row["book_id"], "chapter": row["chapter"]}


def _distinct_evidence(rows: list[dict], cap: int) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in rows:
        key = (row["chunk_id"], row["quote"])
        if not row["quote"] or key in seen:
            continue
        seen.add(key)
        out.append(_evidence(row))
        if len(out) == cap:
            break
    return out


def _predicates(rows: list[dict]) -> list[dict]:
    """[{predicate, count, direction}], most frequent first."""
    counts = Counter((r["predicate"], r["direction"]) for r in rows)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"predicate": p, "count": n, "direction": d} for (p, d), n in ranked]


def _chapter_ref(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"book_id": row["book_id"], "book_title": row["book_title"],
            "chapter": row["chapter"]}


def _locate(stores, entity_id: str, world, spoilers: bool = False):
    """The scope a canonical entity lives in (within `world`), or the
    not_found envelope when it is in none (or, under a reading position, when
    the reader has not met it)."""
    return _entity_scope(stores, entity_id, world, spoilers)[1]


def _canonical(stores, entity_id: str) -> str:
    return canonical_ids(stores.kuzu, [entity_id])[0]


# --- profile ----------------------------------------------------------------------

def _aspect_rank(aspect: str) -> int:
    return DISPLAY_ORDER.index(aspect) if aspect in DISPLAY_ORDER else len(DISPLAY_ORDER)


def _shown_statements(stores, scope, entry, canonical_name: str, name: str) -> list[dict]:
    """The entry's statements the scope lets the reader see, in stored order
    (aspect, then story order), each with its book and chapter. Under a reading
    position an unplaced statement is never shown, and names are respelled as
    the reader has met them."""
    books = scope.books
    where = _chunk_places(stores.conn, [s.chunk_id for s in entry.statements])
    out: list[dict] = []
    for s in entry.statements:
        book_id, chapter = where.get(s.chunk_id, ("", 0))
        if visible.capped(books):  # an unplaced statement cannot be shown safely
            if not book_id or not scope.sees(book_id, chapter):
                continue
        elif book_id and not scope.allows(book_id):
            continue
        text = (visible.respell(s.text, [(canonical_name, name)])
                if visible.capped(books) else s.text)
        out.append({"aspect": s.aspect, "text": text, "quote": s.quote,
                    "chunk_id": s.chunk_id, "book_id": book_id, "chapter": chapter})
    return out


def _intro_input(stores, scope, name: str, kind: str, statements: list[dict],
                 beats: list) -> intro_module.IntroInput:
    """What the intro may be written from: the visible statements and the
    visible beats (names as the reader has met them), both in story order.
    `beats` are already cut to the position."""
    infos = book_rows(stores.conn)
    shown = visible.shown_beats(stores.kuzu, scope.books, beats, infos)
    rows = [{"text": b.text, "chunk_id": b.chunk_id, "book_id": b.book_id,
             "chapter": b.chapter_seq} for b in shown]
    ordered, _note = _in_story_order(rows, infos)
    return intro_module.make_input(name, kind, [(s["aspect"], s["text"]) for s in statements],
                                   [r["text"] for r in ordered])


def _cached_or_fallback(config: LoreConfig, entity_id: str, world_id: int,
                        inp: intro_module.IntroInput) -> tuple[str | None, str]:
    """(intro, source): a cached model intro ("model"), else the deterministic
    one ("fallback"). Reads the cache through a read-only connection, so a
    profile never writes or migrates the library."""
    if inp.empty:
        return None, "none"
    if intro_module.INTRO:
        try:
            conn = sqlite3.connect(f"file:{config.sqlite_path}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                hit = intro_module.cached(conn, entity_id, world_id,
                                          intro_module.scope_hash(inp))
            finally:
                conn.close()
            if hit:
                return hit, "model"
    return intro_module.fallback_intro(inp), "fallback"


@_guarded
def profile(config: LoreConfig, entity_id: str, world: int | str | None = None,
            spoilers: bool = False) -> dict:
    """The entity panel's profile. Aspects come in DISPLAY_ORDER (identity,
    role, nature, abilities, relationships, history, change).

    With no reading position, `summary` is the codex's whole-story summary.
    Under one it is None (`summary_hidden`), and `intro` takes its place: a
    short introduction written only from what the reader has read, the cached
    model intro when there is one, else the deterministic fallback.
    `intro_pending` then says a model intro may be had from `intro()`."""
    with open_stores(config, lance=False) as stores:
        canonical = _canonical(stores, entity_id)
        scope = _locate(stores, canonical, world, spoilers)
        if isinstance(scope, dict):
            return scope
        books = scope.books
        row = _entity_row(stores.kuzu, canonical)
        shown = [b for b in row[3] or [] if books is None or b in books]
        edges = _edge_rows(stores.kuzu, canonical, books)
        beats = [b for b in beats_for_entities(stores.conn, [canonical])
                 if scope.sees(b.book_id, b.chapter_seq)]
        chapters = {(e["book_id"], e["chapter"]) for e in edges}
        chapters |= {(b.book_id, b.chapter_seq) for b in beats}
        name = visible.display_name(_met(stores, books, [canonical]), canonical, row[1] or "")
        out = {"id": row[0], "name": name, "kind": row[2] or "",
               "books": shown, "summary": None, "aspects": [],
               "counts": {"beats": len(beats),
                          "relations": len({e["other"]["id"] for e in edges}),
                          "chapters": len(chapters)}}
        entries = entries_for(stores.conn, [canonical])
        statements: list[dict] = []
        if entries:
            entry = entries[0]
            statements = _shown_statements(stores, scope, entry, row[1] or "", name)
            aspects: dict[str, list[dict]] = {}
            for st in statements:
                aspects.setdefault(st["aspect"], []).append(
                    {k: v for k, v in st.items() if k != "aspect"})
            # Written over the whole story, so hidden while a position is on.
            out["summary"] = None if visible.capped(books) else (entry.summary or None)
            out["aspects"] = [{"aspect": a, "statements": aspects[a]}
                              for a in sorted(aspects, key=_aspect_rank)]
        if visible.capped(books):
            out["summary_hidden"] = True
            inp = _intro_input(stores, scope, name, row[2] or "", statements, beats)
            text, source = _cached_or_fallback(config, canonical, scope.world_id or 0, inp)
            out["intro"] = text
            out["intro_source"] = source
            out["intro_pending"] = source == "fallback" and intro_module.INTRO
        return out


def _intro_model(config: LoreConfig):
    """(generate_json, aclose, model name) on the cheap selection models, or
    None when no Gemini call may be made: the selection step is not on
    Gemini, there is no key, or the ledger says every model is spent. A seam
    for tests."""
    from lore.db.sqlite import open_db
    from lore.inference.gemini import GeminiClient, default_key_file, resolve_api_key
    from lore.orchestrator.gemini_budget import (
        BudgetExceeded, check_budget, recorder_for, today,
    )

    if config.selection.provider != "gemini" or not config.selection.models:
        return None
    conn = open_db(config.sqlite_path)
    try:
        open_models = []
        for model in config.selection.models:
            try:
                check_budget(conn, model, today())
            except BudgetExceeded:
                continue
            open_models.append(model)
    finally:
        conn.close()
    if not open_models:
        return None
    from lore.inference.key_scope import gemini_api_key
    try:  # the bound user's key in multi mode, the local key file otherwise
        api_key = gemini_api_key(local=lambda: resolve_api_key(key_file=default_key_file()))
    except Exception:  # noqa: BLE001 -- no usable key means no model intro
        return None
    if not api_key:
        return None
    client = GeminiClient(api_key, models=open_models,
                          recorder=recorder_for(config.sqlite_path))

    async def generate_json(prompt, schema):
        return await client.generate_json(prompt, schema)
    return generate_json, client.aclose, lambda: client.last_model


@_guarded
def intro(config: LoreConfig, entity_id: str, world: int | str | None = None,
          spoilers: bool = False, model_factory=None) -> dict:
    """The spoiler-safe intro a profile shows under a reading position:
    `{"intro": text | None, "source": "model" | "fallback" | "none"}`.

    Written from the statements and beats the position lets through, and
    nothing else. A cached intro costs nothing; otherwise one cheap model call
    (`model_factory(config)`, default `_intro_model`) writes it and it is
    cached. Off (INTRO), without a model, over budget or on any failure, the
    fallback intro. With no position there is no intro ("none"): the profile
    shows the whole-story summary."""
    import asyncio

    from lore.db.sqlite import open_db

    with open_stores(config, lance=False) as stores:
        canonical = _canonical(stores, entity_id)
        scope = _locate(stores, canonical, world, spoilers)
        if isinstance(scope, dict):
            return scope
        if not visible.capped(scope.books):
            return {"intro": None, "source": "none"}
        row = _entity_row(stores.kuzu, canonical)
        name = visible.display_name(_met(stores, scope.books, [canonical]), canonical,
                                    row[1] or "")
        beats = [b for b in beats_for_entities(stores.conn, [canonical])
                 if scope.sees(b.book_id, b.chapter_seq)]
        entries = entries_for(stores.conn, [canonical])
        statements = (_shown_statements(stores, scope, entries[0], row[1] or "", name)
                      if entries else [])
        inp = _intro_input(stores, scope, name, row[2] or "", statements, beats)
        world_id = scope.world_id or 0
    text, source = _cached_or_fallback(config, canonical, world_id, inp)
    if source != "fallback" or not intro_module.INTRO:
        return {"intro": text, "source": source}
    try:
        made = (model_factory or _intro_model)(config)
    except Exception:  # noqa: BLE001 -- never an error: the fallback stands
        made = None
    if made is None:
        return {"intro": text, "source": "fallback"}
    generate_json, aclose, model_name = made

    async def run() -> str | None:
        try:
            return await intro_module.generate(inp, generate_json)
        finally:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass
    try:
        written = asyncio.run(run())
    except Exception:  # noqa: BLE001
        written = None
    if not written:
        return {"intro": text, "source": "fallback"}
    try:
        conn = open_db(config.sqlite_path)
        try:
            intro_module.save(conn, canonical, world_id, intro_module.scope_hash(inp),
                              written, model_name())
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- an unsaved intro is still shown
        pass
    return {"intro": written, "source": "model"}


def _chunk_places(conn: sqlite3.Connection, chunk_ids: list[str]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    ids = sorted(set(chunk_ids))
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        for cid, book, chapter in conn.execute(
                "SELECT id, book_id, chapter_seq FROM chunks WHERE id IN"
                f" ({','.join('?' * len(batch))})", batch):
            out[cid] = (book, int(chapter))
    return out


# --- timeline ---------------------------------------------------------------------

@_guarded
def timeline(config: LoreConfig, entity_id: str, world: int | str | None = None,
             spoilers: bool = False) -> dict:
    with open_stores(config, lance=False) as stores:
        canonical = _canonical(stores, entity_id)
        scope = _locate(stores, canonical, world, spoilers)
        if isinstance(scope, dict):
            return scope
        rows = []
        seen = [b for b in beats_for_entities(stores.conn, [canonical])
                if scope.sees(b.book_id, b.chapter_seq)]
        for b in visible.shown_beats(stores.kuzu, scope.books, seen, book_rows(stores.conn)):
            others = [{"id": i or None, "name": n}
                      for n, i in zip(b.entities, b.entity_ids or [""] * len(b.entities))
                      if i != canonical]
            rows.append({"kind": b.kind, "text": b.text, "quote": b.quote,
                         "chunk_id": b.chunk_id, "book_id": b.book_id,
                         "chapter": b.chapter_seq, "others": others})
        ordered, note = _in_story_order(rows, book_rows(stores.conn))
        return {"beats": [{
            "kind": r["kind"], "text": r["text"], "quote": r["quote"],
            "chunk_id": r["chunk_id"], "book_id": r["book_id"],
            "book_title": r["book_title"], "chapter": r["chapter"],
            "position_label": r["position_label"], "others": r["others"],
        } for r in ordered], "order_note": note}


# --- relations --------------------------------------------------------------------

@_guarded
def relations(config: LoreConfig, entity_id: str, world: int | str | None = None,
              spoilers: bool = False) -> dict:
    with open_stores(config, lance=False) as stores:
        canonical = _canonical(stores, entity_id)
        scope = _locate(stores, canonical, world, spoilers)
        if isinstance(scope, dict):
            return scope
        edges, note = _in_story_order(_edge_rows(stores.kuzu, canonical, scope.books),
                                      book_rows(stores.conn))
        names = _met(stores, scope.books, {e["other"]["id"] for e in edges})
        for e in edges:
            e["other"] = _shown(names, e["other"])
        grouped: dict[str, list[dict]] = {}
        for e in edges:  # already in story order, so each group is too
            grouped.setdefault(e["other"]["id"], []).append(e)
        ranked = sorted(grouped.values(), key=lambda g: (
            -len(g), g[0]["other"]["name"].casefold(), g[0]["other"]["id"]))
        return {"relations": [{
            "other": g[0]["other"],
            "predicates": _predicates(g),
            "evidence": _distinct_evidence(g, EVIDENCE_PER_RELATION),
            "first_chapter": _chapter_ref(g[0]),
            "last_chapter": _chapter_ref(g[-1]),
        } for g in ranked], "order_note": note}


# --- edge -------------------------------------------------------------------------

@_guarded
def edge(config: LoreConfig, a: str, b: str, world: int | str | None = None,
         spoilers: bool = False) -> dict:
    with open_stores(config, lance=False) as stores:
        ids = []
        scopes = []
        for raw in (a, b):
            canonical = _canonical(stores, raw)
            scope = _locate(stores, canonical, world, spoilers)
            if isinstance(scope, dict):
                return scope
            ids.append(canonical)
            scopes.append(scope)
        # Two worlds never share an edge; with no world named, the first
        # entity's world decides and the second must live in it too.
        if scopes[0].world_id != scopes[1].world_id:
            return error("not_found", f"{b!r} is not in the same world as {a!r}")
        names = _met(stores, scopes[0].books, ids)
        ends = {i: _shown(names, _node(_entity_row(stores.kuzu, i))) for i in ids}
        raw = _edge_rows(stores.kuzu, ids[0], scopes[0].books,
                         other_id=ids[1]) if ids[0] != ids[1] else []
        ordered, note = _in_story_order(raw, book_rows(stores.conn))
        # `direction` is relative to a: "out" is a -> b.
        statements = [{
            "subject": ends[ids[0] if r["direction"] == "out" else ids[1]],
            "object": ends[ids[1] if r["direction"] == "out" else ids[0]],
            "predicate": r["predicate"], "predicate_raw": r["predicate_raw"],
            "quote": r["quote"], "chunk_id": r["chunk_id"],
            "book_id": r["book_id"], "book_title": r["book_title"],
            "chapter": r["chapter"], "position_label": r["position_label"],
        } for r in ordered]
        return {"a": ends[ids[0]], "b": ends[ids[1]], "relationships": statements,
                "predicates": _predicates(ordered), "order_note": note}


# --- search -----------------------------------------------------------------------

def _degrees(kuzu, books) -> dict[str, int]:
    flt, extra = _flt(books)
    where = f" WHERE true{flt}" if flt else ""
    degree: dict[str, int] = {}
    for cypher in (f"MATCH (e:Entity)-[r:REL]->(:Entity){where} RETURN e.id, count(r)",
                   f"MATCH (:Entity)-[r:REL]->(e:Entity){where} RETURN e.id, count(r)"):
        for entity_id, n in kuzu.execute(cypher, extra or None):
            degree[entity_id] = degree.get(entity_id, 0) + int(n)
    return degree


def _match_tier(q: str, name: str, aliases: list[str]) -> tuple[float, str | None] | None:
    """(tier, the alias that matched or None), lower is better; None for no match.

    0 exact name, 1 name prefix, 2 exact alias, 3 alias prefix, 4 a word of the
    name or an alias starts with it, 5 it appears inside, 6+ a close
    misspelling (the closer, the lower)."""
    n = name.casefold()
    alias_cf = [(al.casefold(), al) for al in aliases if al]
    if n == q:
        return 0, None
    if n.startswith(q):
        return 1, None
    for al, raw in alias_cf:
        if al == q:
            return 2, raw
    for al, raw in alias_cf:
        if al.startswith(q):
            return 3, raw
    forms = [(n, None)] + alias_cf
    for form, raw in forms:
        if any(w.startswith(q) for w in form.split()):
            return 4, raw
    for form, raw in forms:
        if q in form:
            return 5, raw
    if len(q) >= FUZZY_MIN_CHARS:
        best, best_raw = 0.0, None
        for form, raw in forms:
            for cand in {form, *form.split()}:
                ratio = difflib.SequenceMatcher(None, q, cand).ratio()
                if ratio > best:
                    best, best_raw = ratio, raw
        if best >= FUZZY_MIN_RATIO:
            return 6 + (1 - best), best_raw
    return None


@_guarded
def search(config: LoreConfig, q: str, world: int | str | None = None,
           limit: int = SEARCH_LIMIT_DEFAULT, spoilers: bool = False) -> dict:
    needle = " ".join((q or "").split()).casefold()
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    if not needle:
        return {"results": []}
    with open_stores(config, lance=False) as stores:
        try:
            scopes = _scoped(stores, world, spoilers)
        except UnknownWorld as exc:
            return _world_error(exc)
        stubs = {r[0] for r in stores.kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(:Entity) RETURN a.id")}
        everyone = [r for r in stores.kuzu.execute(
            "MATCH (e:Entity) RETURN e.id, e.name, e.kind, e.books, e.aliases")
            if r[0] not in stubs]
        ranked = []
        for scope in scopes:
            degree = _degrees(stores.kuzu, scope.books)
            # Under a reading position only the names the reader has met are
            # searched (never the merged canonical name or an unmet alias), and
            # only entities met so far are found.
            mine = [r for r in everyone if scope.allows_any(list(r[3] or []))]
            names = (_met(stores, scope.books, [r[0] for r in mine])
                     if visible.capped(scope.books) else None)
            for r in mine:
                books = list(r[3] or [])
                if names is None:
                    name, aliases = r[1] or "", list(r[4] or [])
                elif r[0] in names:
                    name, aliases = names[r[0]][0], names[r[0]][1:]
                else:
                    continue
                hit = _match_tier(needle, name, aliases)
                if hit is None:
                    continue
                tier, alias = hit
                item = {"id": r[0], "name": name, "kind": r[2] or "",
                        "books": [b for b in books if scope.allows(b)],
                        "matched_alias": alias}
                if len(scopes) > 1:
                    item["world"] = scope.tag()
                ranked.append(((tier, -degree.get(r[0], 0), item["name"].casefold(),
                                r[0]), item))
        ranked.sort(key=lambda t: t[0])
        return {"results": [item for _k, item in ranked[:limit]]}


# --- path -------------------------------------------------------------------------

def _neighbours(kuzu, frontier: list[str], books) -> dict[str, set[str]]:
    flt, extra = _flt(books)
    found: dict[str, set[str]] = {}
    for r in kuzu.execute(
            f"MATCH (e:Entity)-[r:REL]-(b:Entity) WHERE e.id IN $ids AND b.id <> e.id"
            f"{flt} RETURN e.id, b.id, b.books, count(r)", {"ids": frontier, **extra}):
        if books is not None and not any(x in books for x in r[2] or ()):
            continue
        found.setdefault(r[0], set()).add(r[1])
    return found


def _shortest_path(kuzu, start: str, goal: str, books, max_hops: int,
                   deadline: float) -> tuple[list[str] | None, bool]:
    """(the entity ids from start to goal, timed_out). A level-by-level BFS
    over REL edges either way round; ties go to the lowest ids, so the same
    question always gives the same path."""
    if start == goal:
        return [start], False
    parent: dict[str, str] = {start: ""}
    frontier = [start]
    for _hop in range(max_hops):
        if time.monotonic() > deadline:
            return None, True
        nxt: list[str] = []
        reached = _neighbours(kuzu, frontier, books)
        for node in frontier:  # frontier is sorted
            for other in sorted(reached.get(node, ())):
                if other not in parent:
                    parent[other] = node
                    nxt.append(other)
        if goal in parent:
            path = [goal]
            while parent[path[-1]]:
                path.append(parent[path[-1]])
            return path[::-1], False
        if not nxt:
            return None, False
        frontier = sorted(nxt)
    return None, False


@_guarded
def path(config: LoreConfig, a: str, b: str, world: int | str | None = None,
         max_hops: int = PATH_MAX_HOPS, spoilers: bool = False) -> dict:
    max_hops = max(1, min(int(max_hops), PATH_MAX_HOPS))
    with open_stores(config, lance=False) as stores:
        ids, scopes = [], []
        for raw in (a, b):
            canonical = _canonical(stores, raw)
            scope = _locate(stores, canonical, world, spoilers)
            if isinstance(scope, dict):
                return scope
            ids.append(canonical)
            scopes.append(scope)
        none = {"nodes": [], "hops": [], "found": False, "timed_out": False}
        if scopes[0].world_id != scopes[1].world_id:
            return none
        found, timed_out = _shortest_path(
            stores.kuzu, ids[0], ids[1], scopes[0].books, max_hops,
            time.monotonic() + PATH_TIME_CAP_S)
        if not found:
            return {**none, "timed_out": timed_out}
        names = _met(stores, scopes[0].books, found)
        info = {r[0]: _shown(names, _node(r)) for r in stores.kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name, e.kind",
            {"ids": found})}
        books = book_rows(stores.conn)
        hops = []
        for left, right in zip(found, found[1:]):
            ordered, _note = _in_story_order(
                _edge_rows(stores.kuzu, left, scopes[0].books, other_id=right), books)
            hops.append({"from": left, "to": right,
                         "predicates": _predicates(ordered),
                         "evidence": _distinct_evidence(ordered, EVIDENCE_PER_HOP)})
        return {"nodes": [info[i] for i in found], "hops": hops, "found": True,
                "timed_out": False}


# --- passage ----------------------------------------------------------------------

@_guarded
def passage(config: LoreConfig, chunk_id: str, world: int | str | None = None,
            spoilers: bool = False) -> dict:
    with open_stores(config, kuzu=False, lance=False) as stores:
        try:
            books = world_books(stores.conn, world, spoilers=spoilers)
        except UnknownWorld as exc:
            return _world_error(exc)
        row = stores.conn.execute(
            "SELECT id, book_id, chapter_seq, text FROM chunks WHERE id = ?",
            (chunk_id,)).fetchone()
        if row is None or not visible.sees(books, row[1], int(row[2])):
            return error("not_found", f"no passage {chunk_id!r}" + _in_world(world))
        info = book_rows(stores.conn).get(row[1])
        positioned, _note = _in_story_order(
            [{"chunk_id": row[0], "book_id": row[1], "chapter": int(row[2])}],
            book_rows(stores.conn))
        return {"chunk_id": row[0], "book_id": row[1],
                "book_title": info.title if info else row[1],
                "chapter": int(row[2]), "text": row[3],
                "position_label": positioned[0]["position_label"]}
