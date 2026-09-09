"""Load resolved triplets into Kuzu and LanceDB, one book at a time.

Idempotent by construction: nodes are MERGEd (so an entity shared with
another book survives a reload), while this book's REL and MENTIONS edges
and its LanceDB chunk rows are deleted and rewritten, because
re-extraction can legitimately change them.

Entity rows are never written as if this book were their only writer:

  * an entity this book wrote last time but does not write now is stale.
    If this book was its only owner it is DETACH DELETEd from Kuzu and
    dropped from LanceDB; if other books also claim it, only this book's
    entry in `books` is removed and the node is kept.
  * an entity's LanceDB vector is the centroid of EVERY chunk that
    mentions it across EVERY book it belongs to. Other books' chunk rows
    are read back from the persisted `chunks` table and this book's come
    from the rows about to replace its own, so the value is exact and does
    not depend on which book was loaded last.

Cross-book identity — `entity_id` is a pure hash of (normalized name,
kind) with no book in it, and `resolve_mentions` only ever sees one book's
mentions per call, so "MERGE the node and move on" would silently fuse two
unrelated books' homonyms at the persistence seam, exactly the merge the
resolver's `cross_book_floor`/pass-3 gate exists to prevent. The Entity
node therefore carries a `books` list, and `_reconcile_ids` below decides,
before anything is written, which node each incoming entity may own:

  * a persisted node whose `books` already contains this book_id is this
    same book rewriting itself — fused with NO adjudication, so a re-load
    is free and deterministic;
  * a persisted node belonging to some *other* book is fused into only if
    the shared name is distinctive (`is_generic_name` is False). An id
    collision means the two normalized names are IDENTICAL, which is not a
    judgement call; what matters is whether such a name can name two
    different people, and a stoplist answers that deterministically. This
    deliberately replaces a call to `adjudicate` — see `_reconcile_ids`;
  * a persisted node whose name is a VARIANT of the candidate's — one
    name's identity tokens nesting inside the other's, `watson` inside
    `john watson` — is fused into on the same terms, provided the match is
    unambiguous in both directions. There is no id collision to key on
    here, so `_reconcile_ids` searches the persisted names; Task 1i, and
    the only path by which Watson, a prime cross-book question subject,
    becomes one entity across the two novels;
  * a refusal mints the next id in Task 3's collision chain
    (`entity_id_chain`), so both entities survive as separate nodes with
    their own edges.

Failure containment — the two stores have no shared transaction, so the
write is staged in three phases and ordered so that the largest, most
failure-prone body of work sits under the one real transaction available:

  1. Read and compute. Artifacts, registry, resolution, id reconciliation
     (reads and adjudication only), predicate normalization, entity
     centroids and the *entire* Kuzu statement plan are built in memory.
     Nothing has been mutated, so any failure here (a missing artifact, a
     truncated embedder response, an unresolvable book) leaves both stores
     exactly as they were.
  2. Kuzu, inside one explicit transaction. The book's old REL/MENTIONS
     edges are deleted and the new graph written in a single BEGIN/COMMIT.
     A failure rolls the whole thing back: the previous graph survives.
  3. LanceDB, last. Delete-then-add per table, each delete immediately
     followed by its add, with the rows already built. LanceDB offers no
     transaction spanning a delete and an add, so a failure here — and
     only here — can leave the vector store short of this book's rows
     while Kuzu already holds the new graph. That is raised as
     `PartialLoadError`, never swallowed; re-running `load_book` repairs
     it, because the load is deterministic and idempotent.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from itertools import islice

from lore.config import LoreConfig
from lore.db.kuzu_store import KuzuStore
from lore.db.lance_store import LanceStore
from lore.graph.artifacts import completed_unit_dirs, read_embeddings, read_triplets
from lore.ingest.registry import chunks_for_book
from lore.resolve.disambiguation import DisambiguationStore
from lore.resolve.entityhood import (
    is_generic_name,
    is_gutenberg_boilerplate,
    is_probable_entity,
)
from lore.resolve.normalize import entity_id, normalize_name
from lore.resolve.predicates import normalize_predicates
from lore.resolve.resolver import (
    Adjudicate,
    Mention,
    display_name,
    entity_id_chain,
    is_description_shaped,
    name_tokens,
    resolve_mentions,
    tokens_nest,
)

Statement = tuple[str, dict]

# How far down a name's collision chain a load will look for a free slot.
# 64 distinct entities sharing one normalized name and kind across a whole
# corpus is not a real library; it is a bug, and it fails loudly.
_MAX_HOMONYMS = 64


@dataclass
class LoadReport:
    book_id: str
    entities: int = 0
    alias_links: int = 0
    rel_edges: int = 0
    chunks: int = 0
    mentions: int = 0
    dropped_edges: int = 0
    dropped_mentions: int = 0
    boilerplate_chunks: int = 0


class PartialLoadError(RuntimeError):
    """The graph was written but the vector store was left incomplete.

    The only genuinely non-atomic window in the load. Loud on purpose:
    the caller must re-run the load rather than treat the book as ready.
    """


@dataclass(frozen=True)
class _PersistedEntity:
    """An Entity node already in Kuzu, possibly put there by another book."""

    id: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    books: tuple[str, ...]


@dataclass(frozen=True)
class _NodeCandidate:
    """An Entity node this load wants to write, before id reconciliation."""

    id: str
    base: str  # the normalized name its id was derived from
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    books: tuple[str, ...] = ()
    context: str = ""
    # True for a node that exists only to carry one surface form of another
    # entity (ALIAS_OF). Not an entity in its own right, so it takes no part
    # in the cross-book name-variant search below — as a match it would be a
    # duplicate of its own canonical, and counting it would make every real
    # variant pair look ambiguous.
    is_alias: bool = False


def _base_normalized(node_id: str, name: str, kind: str, aliases: tuple) -> str:
    """The normalized string whose hash produced `node_id`.

    Needed to walk that id's collision chain. An entity's aliases are its
    normalized forms, so the one that hashes back to the id is the base;
    if the resolver already suffixed the id, none will, and the entity's
    own normalized display name is a stable fallback.
    """
    for alias in sorted(aliases):
        if entity_id(alias, kind) == node_id:
            return alias
    return normalize_name(name) or name


def _named_kinds(
    row: dict, on_reject: Callable[[str], None] | None = None
) -> list[tuple[str, str]]:
    """The (name, kind) pairs a triplet row names, junk mentions dropped.

    A name with no word characters normalizes to "" and mints no entity
    (see `normalize_name`); minting a node for it here would put a junk
    entity in the graph, so such names are skipped at the source. The same
    goes for a name that normalizes fine but is not a probable ENTITY at
    all — a definite description like "the box" or "his hands" that the
    extraction prompt happily copied verbatim (see `is_probable_entity`).

    `on_reject`, when given, is called once per name skipped here, so a
    caller can count drops without this function reaching into a report
    object itself. It is deliberately optional and only wired from the one
    call site that scans every row exactly once (`_mentions_from`) — the
    second call site below re-derives the same pairs from already-resolved
    mentions, and counting there too would double every drop.
    """
    names = list(row.get("entity_names") or [])
    kinds = list(row.get("entity_kinds") or [])
    pairs: list[tuple[str, str]] = []
    for index, name in enumerate(names):
        text = str(name)
        if not normalize_name(text) or not is_probable_entity(text):
            if on_reject is not None:
                on_reject(text)
            continue
        pairs.append((text, str(kinds[index]) if index < len(kinds) else "Concept"))
    return pairs


def _mentions_from(
    rows: list[dict], book_id: str, on_reject: Callable[[str], None] | None = None
) -> list[Mention]:
    """One Mention per (name, kind) pair the triplet rows name, with an
    evidence sentence as local context for the embedding pass.

    The context must be derived from the CONTENT of the rows, never from
    their order. `resolve_mentions` is deliberately a function of the
    mention SET — it sorts by (book, normalized name, kind, context)
    before doing anything — and that context also feeds the pass-2
    embedding text and the pass-3 prompt. Keeping "whichever row happened
    to come first" would make the whole resolution a function of the input
    sequence again: a `--resume` rerun of one unit, or a changed
    `work_unit_size` moving unit boundaries, reorders rows and would then
    silently produce a different graph from identical data.

    Of the two content-derived choices, the lexicographically smallest
    evidence is used rather than a concatenation of all of them: it is
    a single real sentence of bounded length, so it neither grows with
    corpus size nor risks being truncated by the embedder's context
    window — a truncation that would quietly reintroduce a dependence on
    which evidences happened to land inside the window.
    """
    contexts: dict[tuple[str, str], str] = {}
    for row in rows:
        evidence = str(row.get("evidence") or "")
        for pair in _named_kinds(row, on_reject):
            current = contexts.get(pair)
            if current is None or evidence < current:
                contexts[pair] = evidence
    return [
        Mention(name=name, kind=kind, book_id=book_id, context=context)
        for (name, kind), context in sorted(contexts.items())
    ]


def _centroid(vectors: list[list[float]]) -> list[float]:
    total = [0.0] * len(vectors[0])
    for vector in vectors:
        for index, value in enumerate(vector):
            total[index] += float(value)
    return [value / len(vectors) for value in total]


def _entities_where(
    kuzu: KuzuStore, predicate: str, params: dict
) -> dict[str, _PersistedEntity]:
    """Every Entity node matching `predicate`, by id.

    The two callers below differ only in that WHERE clause, so the row
    shape and its unpacking live here once.
    """
    return {
        row[0]: _PersistedEntity(
            id=row[0], name=row[1], kind=row[2],
            aliases=tuple(row[3] or ()), books=tuple(row[4] or ()),
        )
        for row in kuzu.execute(
            f"MATCH (e:Entity) WHERE {predicate}"
            " RETURN e.id, e.name, e.kind, e.aliases, e.books",
            params,
        )
    }


def _persisted_entities(
    kuzu: KuzuStore, ids: Iterable[str]
) -> dict[str, _PersistedEntity]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    return _entities_where(kuzu, "e.id IN $ids", {"ids": wanted})


def _entities_of_kinds(
    kuzu: KuzuStore, kinds: Iterable[str]
) -> dict[str, _PersistedEntity]:
    """Every Entity node of any of these kinds.

    A full scan of the kinds in play, because Task 1i's cross-book name
    VARIANT rule has no id to key on: `watson` and `john watson` hash
    differently, so the only way to find the counterpart is to look at the
    persisted names. The graph is one node per entity per corpus (378 for
    the two novels), so this is one small query per load; if a corpus ever
    grows big enough for it to matter, the fix is a token index, not a
    weaker rule.
    """
    wanted = sorted(set(kinds))
    if not wanted:
        return {}
    return _entities_where(kuzu, "e.kind IN $kinds", {"kinds": wanted})


def _alias_node_ids(kuzu: KuzuStore) -> set[str]:
    """Every Entity node that exists only as another entity's surface form.

    An ALIAS_OF source is not an entity in its own right — `Watson` and
    `Dr. John Watson` are one person and one of them is only a spelling. It
    must therefore take no part in the cross-book variant search: as a
    match it is a duplicate of its own canonical, and counting it would
    make every genuine variant pair look ambiguous and refuse it.
    """
    return {
        row[0]
        for row in kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(:Entity) RETURN DISTINCT a.id"
        )
    }


def _name_forms(base: str, aliases: tuple[str, ...]) -> list[frozenset[str]]:
    """The identity-token sets of every normalized name an entity answers to.

    Empty (and so never a variant match) when any one of those names has no
    identity-bearing token at all — `tokens_nest` refuses an empty side, and
    a name made of nothing but function words must not nest inside an
    arbitrary other name.
    """
    forms = [name_tokens(form) for form in sorted({base, *aliases}) if form]
    return forms if forms and all(forms) else []


def _is_name_variant(
    left: list[frozenset[str]], right: list[frozenset[str]]
) -> bool:
    """True when two entities are the same name written two ways.

    EVERY name form on one side must nest with EVERY form on the other, not
    merely one of them: that is Task 1g's hard-won requirement, lifted from
    the same-book path (`resolver.resolve_mentions`, pass 2a) to this one.
    A pairwise version lets a merged entity bridge two names that never
    nested with each other — measured on the live corpus, `mary` +
    `mary morstan` then admitted `morstan`, fusing Mary Morstan with her
    father Captain Morstan.
    """
    return bool(left) and bool(right) and all(
        tokens_nest(a, b) for a in left for b in right
    )


def _swallows_a_name(
    left: list[frozenset[str]], left_name: str,
    right: list[frozenset[str]], right_name: str,
) -> bool:
    """True when the SUPERSET side of a variant match is a DESCRIPTION.

    Task 1k, the cross-book half. `foreigners in England` nests around
    `england` and won it, and three of the eleven cross-book entities on
    the rebuilt graph were junk of exactly this shape. A description may
    exist as a node; it may not absorb the name inside it.

    Both names here are RAW display names — `display_name` keeps the
    longest raw surface form, which for a nesting pair is the superset
    side, and the possessive that gives a description away survives only
    there ("Lucy Ferrier's father" normalizes to `lucy ferrier father`).
    An entity's persisted aliases are normalized, so they cannot be tested
    and are not: the display name is the one that matters, because it is
    the form that would end up naming the merged node.

    Called only for pairs `_is_name_variant` already accepted, so every
    form on each side nests with every form on the other and the union of
    each side's tokens orders them. Equal unions are the same name written
    two ways — neither swallows the other.
    """
    left_tokens = frozenset().union(*left)
    right_tokens = frozenset().union(*right)
    if left_tokens < right_tokens:
        return is_description_shaped(right_name)
    if right_tokens < left_tokens:
        return is_description_shaped(left_name)
    return False


def _entities_owned_by(kuzu: KuzuStore, book_id: str) -> dict[str, _PersistedEntity]:
    """Every Entity node whose `books` currently names this book.

    A re-extraction can drop an entity the previous load wrote. Without
    this the node survives with no MENTIONS and no REL but still claiming
    the book, and "who is in book X" answers with characters the book no
    longer contains.
    """
    return _entities_where(
        kuzu, "list_contains(e.books, $book)", {"book": book_id}
    )


async def _reconcile_ids(
    kuzu: KuzuStore,
    book_id: str,
    candidates: list[_NodeCandidate],
) -> tuple[dict[str, str], dict[str, _PersistedEntity]]:
    """Decide which persisted node, if any, each candidate may own.

    Read-only: it queries Kuzu but writes nothing, so it stays inside phase
    1's "no store mutated" guarantee.

    Task 1h took the cross-book decision away from `adjudicate`. It is still
    the resolver's pass-3 tie-breaker for *same-book* mentions whose names
    differ (see `resolve_mentions`), and is still wired through `load_book`
    to reach it; it is simply not asked here any more, because on the
    rebuilt two-novel graph it answered anti-correlated with the truth —
    it merged `the police Inspector` across the two books (Lestrade in one,
    Athelney Jones in the other) and refused `Sherlock Holmes` <->
    `Mr. Sherlock Holmes`, which normalize to the same string. Two rounds
    of prompt tuning did not move it, so every case this function sees is
    now decided by rule and none are left over for the model. It stays
    `async` so that re-introducing an awaited decision here — should a
    trustworthy adjudicator ever exist — costs no call-site churn.

    Task 1i added step 2b, the cross-book name VARIANT. Steps 1 and 2 only
    ever look at a candidate's own id chain, so they see a cross-book pair
    only when the two normalized names are identical; `watson` and
    `john watson` hash differently and were never considered. Step 2b
    searches the persisted names instead, which is why this function now
    reads the whole Entity table for the kinds in play. Every decision it
    makes is still a pure function of the two books' data.
    """
    chains = {
        candidate.id: list(
            islice(entity_id_chain(candidate.base, candidate.kind), _MAX_HOMONYMS)
        )
        for candidate in candidates
    }
    persisted = _persisted_entities(kuzu, (c.id for c in candidates))
    # Only a candidate whose base id is held by a *different* book can need
    # more of its chain, so the second lookup stays small.
    contested = [
        candidate for candidate in candidates
        if candidate.id in persisted
        and book_id not in persisted[candidate.id].books
    ]
    if contested:
        persisted.update(_persisted_entities(
            kuzu,
            (cid for candidate in contested for cid in chains[candidate.id][1:]),
        ))

    # Task 1i: the cross-book name VARIANT index. Every persisted node of a
    # kind this load writes, with its normalized name forms precomputed, so
    # step 2b below is a pure in-memory scan. Generic names are dropped
    # here rather than at match time — a generic name can name two
    # different people, which is exactly what the Task 1h stoplist is for,
    # and that is true whichever side of the match it sits on.
    searchable = _entities_of_kinds(kuzu, (c.kind for c in candidates))
    alias_nodes = _alias_node_ids(kuzu)
    variant_index: dict[str, list[tuple[str, str, list[frozenset[str]]]]] = {}
    for node in searchable.values():
        node_base = _base_normalized(node.id, node.name, node.kind, node.aliases)
        if node.id in alias_nodes or is_generic_name(node_base):
            continue
        forms = _name_forms(node_base, node.aliases)
        if forms:
            variant_index.setdefault(node.kind, []).append(
                (node_base, node.id, forms)
            )
    # Sorted by (normalized name, id) so every list below is walked in a
    # content-derived order and never in the order Kuzu returned the rows.
    for rows in variant_index.values():
        rows.sort(key=lambda row: (row[0], row[1]))

    # Every variant match in both directions, computed before any candidate
    # is decided, because the rule is MUTUAL uniqueness: a match may only
    # merge when this candidate has exactly one persisted partner AND that
    # partner has exactly one candidate. `sholto` nesting inside both
    # `thaddeus sholto` and `bartholomew sholto` is two sons and their
    # father, not one man; so is one persisted `sholto` matched by two
    # incoming Sholtos. Both directions have to be checked, or the second
    # shape merges whichever candidate happens to be reached first.
    variant_matches: dict[str, list[tuple[str, str]]] = {}
    matched_by: dict[str, int] = {}
    for candidate in candidates:
        if candidate.is_alias or is_generic_name(candidate.base):
            continue
        forms = _name_forms(candidate.base, candidate.aliases)
        if not forms:
            continue
        found = [
            (node_base, node_id)
            for node_base, node_id, node_forms in variant_index.get(
                candidate.kind, ()
            )
            # An id on this candidate's own chain was already ruled on by
            # steps 1 and 2; re-deciding it here would undo that ruling.
            if node_id not in chains[candidate.id]
            and _is_name_variant(forms, node_forms)
            # Task 1k: and the longer of the two names is not a definite
            # description swallowing the shorter one. Dropped here rather
            # than at match time so it is also uncounted in `matched_by`
            # below — a merge that can never be made must not be what makes
            # a real one look ambiguous.
            and not _swallows_a_name(
                forms, candidate.name, node_forms, searchable[node_id].name
            )
        ]
        if found:
            variant_matches[candidate.id] = found
            for _node_base, node_id in found:
                matched_by[node_id] = matched_by.get(node_id, 0) + 1

    id_for: dict[str, str] = {}
    claimed: set[str] = set()
    for candidate in candidates:
        chain = chains[candidate.id]

        # 1. This book already owns a node in this chain: it is rewriting
        #    itself. Fuse, and never re-adjudicate — a re-load must be free.
        chosen = next(
            (cid for cid in chain
             if cid not in claimed
             and cid in persisted
             and book_id in persisted[cid].books),
            None,
        )

        # 2. Otherwise every persisted homonym belongs to another book, and
        #    all of them share this candidate's normalized name. Distinctive
        #    names ("sherlock holmes") are the same entity in both books, so
        #    fuse into the first free one. Generic names ("police inspector",
        #    "captain") can name two different people, so fuse into none of
        #    them and fall through to step 3 — which is Phase 2B's stated
        #    intent, "the Captain in two unrelated novels stays two people",
        #    now held by a rule instead of by a model that inverted it.
        if chosen is None and not is_generic_name(candidate.base):
            chosen = next(
                (cid for cid in chain
                 if cid in persisted and cid not in claimed),
                None,
            )

        # 2b. Task 1i — a cross-book name VARIANT. There is no id collision
        #    to key on: `watson` and `john watson` hash differently, so
        #    step 2 never sees them. Search the persisted names instead and
        #    apply the SAME rule the same-book path uses (Task 1g): if one
        #    name's identity tokens nest inside the other's, and the name is
        #    distinctive, they are one entity. `Watson` is a prime
        #    cross-book question subject and this is the only step that
        #    reaches him.
        #
        #    Deliberately NOT restricted to nodes another book owns. A
        #    variant merge adopts an id derived from the OTHER book's
        #    spelling, which this book will not mint again next time, so a
        #    reload must be able to find the node it merged into — step 1
        #    cannot, because that id is not on this candidate's chain.
        #    Reaching a node this book already owns is therefore what makes
        #    the merge idempotent, and it is preferred over a foreign one
        #    for the same reason.
        #
        #    Only a candidate that got past steps 1 and 2 arrives here, so a
        #    same-book entity that still owns its own node is never a
        #    target: it was already claimed in step 1.
        #
        #    The match must be MUTUALLY unique (see `variant_matches`): a
        #    bare surname that fits two full names, or a full name that two
        #    incoming names fit, is a family and not a person.
        if chosen is None:
            found = variant_matches.get(candidate.id, ())
            owned = [m for m in found if book_id in searchable[m[1]].books]
            pick: str | None = None
            if len(owned) == 1:
                # A node this book is already part of: this is the reload
                # path, re-finding the merge this book made last time. Not a
                # new decision, so the uniqueness test does not apply — the
                # node's `books` already records the answer.
                pick = owned[0][1]
            elif not owned and len(found) == 1 and matched_by[found[0][1]] == 1:
                pick = found[0][1]
            if pick is not None and pick not in claimed:
                chosen = pick
                persisted.setdefault(chosen, searchable[chosen])

        # 3. Refused (or nothing to fuse with): take the next free slot, so
        #    both entities survive as separate nodes with their own edges.
        if chosen is None:
            chosen = next(
                (cid for cid in chain
                 if cid not in persisted and cid not in claimed),
                None,
            )
        if chosen is None:
            raise RuntimeError(
                f"more than {_MAX_HOMONYMS} distinct entities named"
                f" {candidate.name!r} of kind {candidate.kind!r}; refusing"
                " to load rather than fuse them silently"
            )
        claimed.add(chosen)
        id_for[candidate.id] = chosen
    return id_for, persisted


def _run_in_transaction(kuzu: KuzuStore, statements: list[Statement]) -> None:
    kuzu.execute("BEGIN TRANSACTION")
    try:
        for cypher, params in statements:
            kuzu.execute(cypher, params)
        kuzu.execute("COMMIT")
    except BaseException:
        try:
            kuzu.execute("ROLLBACK")
        except Exception:
            # Kuzu aborts the transaction itself when one of its own
            # statements fails, and then refuses an explicit ROLLBACK.
            # Either way the transaction is not committed.
            pass
        raise


async def load_book(
    config: LoreConfig,
    conn: sqlite3.Connection,
    kuzu: KuzuStore,
    lance: LanceStore,
    book_id: str,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]],
    adjudicate: Adjudicate,
    disambiguation: DisambiguationStore | None = None,
) -> LoadReport:
    """Load one book. `disambiguation` is Task 1j's optional residue store.

    When it is None — the default, and what `lore process` passes unless
    `[lore.disambiguation] enabled = true` — nothing about this load
    differs from before Task 1j: no questions are recorded, no cache is
    consulted, no directory is created, and no network call is possible.

    When it is given, the load is still entirely offline. It only WRITES
    the ambiguous merges it refused (so `lore disambiguate` can spend one
    request on all of them at once) and READS answers a previous run
    already paid for.
    """
    # ---- Phase 1: read and compute. No store is mutated below this line.
    dirs = completed_unit_dirs(conn, config, book_id)
    if not dirs:
        raise ValueError(f"no completed extraction units for book {book_id}")

    triplets = read_triplets(dirs)
    embeddings = {row["chunk_id"]: list(row["vector"]) for row in read_embeddings(dirs)}
    chunks = chunks_for_book(conn, book_id)
    chapter_of = {chunk.id: chunk.chapter_seq for chunk in chunks}

    # Task 1i defect 2. Gutenberg's header, table of contents and licence
    # are ingested as ordinary chapters, and their boilerplate minted
    # eleven of the eighteen entities that spanned both novels — they span
    # both books precisely because the text is identical in both. Fixing it
    # at ingest means re-extracting the corpus, so the rows are dropped
    # here instead, before they can mint an entity, an edge or a MENTIONS.
    # The Chunk nodes and their vectors are left alone: they are the
    # registry's faithful record of the file, and with no entity on them
    # the graph never reaches the text.
    boilerplate = {
        chunk.id for chunk in chunks if is_gutenberg_boilerplate(chunk.text)
    }
    if boilerplate:
        triplets = [
            row for row in triplets
            if str(row.get("chunk_id") or "") not in boilerplate
        ]

    dropped_mentions = 0

    def _count_rejected(_name: str) -> None:
        nonlocal dropped_mentions
        dropped_mentions += 1

    mentions = _mentions_from(triplets, book_id, _count_rejected)
    resolution = await resolve_mentions(
        mentions, embed, adjudicate,
        record_ambiguous=None if disambiguation is None else disambiguation.record,
        decide_ambiguous=(
            None if disambiguation is None else disambiguation.answer_for
        ),
    )
    predicate_map = await normalize_predicates(
        [str(row.get("predicate") or "") for row in triplets], embed
    )

    # A representative context per entity, for adjudicating it against a
    # node another book persisted. Picked in the resolver's own canonical
    # order so it does not depend on artifact read order.
    context_for: dict[str, str] = {}
    for mention in sorted(
        mentions, key=lambda m: (normalize_name(m.name), m.kind, m.context)
    ):
        target = resolution.mention_map.get(
            (mention.name, mention.kind, mention.book_id)
        )
        if target is not None:
            context_for.setdefault(target, mention.context)

    candidates: list[_NodeCandidate] = [
        _NodeCandidate(
            id=entity.id,
            base=_base_normalized(entity.id, entity.name, entity.kind,
                                  entity.aliases),
            name=entity.name, kind=entity.kind, aliases=entity.aliases,
            books=entity.book_ids or (book_id,),
            context=context_for.get(entity.id, ""),
        )
        for entity in resolution.entities
    ]
    alias_records = [
        alias for alias in resolution.aliases if alias.alias_id != alias.canonical_id
    ]
    seen_ids = {candidate.id for candidate in candidates}
    for alias in alias_records:
        # An alias surface form only gets its own node if no canonical
        # already claims that id.
        if alias.alias_id in seen_ids:
            continue
        seen_ids.add(alias.alias_id)
        candidates.append(_NodeCandidate(
            id=alias.alias_id,
            base=normalize_name(alias.alias_name),
            name=alias.alias_name, kind=alias.kind,
            books=(book_id,),
            context=context_for.get(alias.canonical_id, ""),
            is_alias=True,
        ))

    id_for, persisted = await _reconcile_ids(kuzu, book_id, candidates)

    node_writes: dict[str, dict] = {}
    for candidate in candidates:
        target = id_for[candidate.id]
        node = persisted.get(target)
        # `node` is present only for a same-book rewrite or an approved
        # cross-book merge; a refusal minted a fresh id, so nothing of the
        # other book's entity leaks into it.
        node_writes[target] = {
            "id": target,
            "name": display_name(
                [candidate.name] + ([node.name] if node and node.name else [])
            ),
            "kind": candidate.kind,
            "aliases": sorted(set(candidate.aliases) | set(node.aliases if node else ())),
            "books": sorted(set(candidate.books) | set(node.books if node else ())),
        }

    # An entity this book wrote last time but does not write now is stale.
    # If this book was its only owner it goes away entirely; if it is
    # shared, only this book's claim on it does.
    orphan_ids: list[str] = []
    released: dict[str, _PersistedEntity] = {}
    for node_id, node in _entities_owned_by(kuzu, book_id).items():
        if node_id in node_writes:
            continue
        remaining = tuple(book for book in node.books if book != book_id)
        if remaining:
            released[node_id] = replace(node, books=remaining)
        else:
            orphan_ids.append(node_id)
    orphan_ids.sort()

    entity_for = {
        key: id_for.get(target, target)
        for key, target in resolution.mention_map.items()
    }
    alias_statements: list[Statement] = []
    for alias in alias_records:
        alias_target = id_for.get(alias.alias_id, alias.alias_id)
        canonical_target = id_for.get(alias.canonical_id, alias.canonical_id)
        if alias_target == canonical_target:
            continue
        alias_statements.append((
            "MATCH (a:Entity {id: $alias}), (c:Entity {id: $canonical})"
            " MERGE (a)-[:ALIAS_OF]->(c)",
            {"alias": alias_target, "canonical": canonical_target},
        ))

    # Surface form -> entity id, for a subject/object the row did not list
    # among its entity_names. First match wins: if the same surface name
    # was mentioned under two different kinds, the one the resolver saw
    # first (its own canonical ordering, not our read order) is used.
    by_surface: dict[str, str] = {}
    for (name, _kind, _book), target in entity_for.items():
        by_surface.setdefault(name, target)

    row = conn.execute(
        "SELECT title, author FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    title, author = (row[0], row[1] or "") if row else ("", "")

    report = LoadReport(book_id=book_id, entities=len(resolution.entities),
                        alias_links=len(resolution.aliases),
                        dropped_mentions=dropped_mentions,
                        boilerplate_chunks=len(boilerplate))

    # Which canonical entities each chunk mentions — the bridge LanceDB
    # rows carry, and the source of the MENTIONS edges.
    chunk_entities: dict[str, set[str]] = {}
    rel_statements: list[Statement] = []
    for row in triplets:
        chunk_id = str(row["chunk_id"])
        per_row: dict[str, str] = {}
        for name, kind in _named_kinds(row):
            target = entity_for.get((name, kind, book_id))
            if target is None:
                continue
            per_row.setdefault(name, target)
            chunk_entities.setdefault(chunk_id, set()).add(target)

        subject = str(row.get("subject") or "")
        obj = str(row.get("object") or "")
        subject_id = per_row.get(subject, by_surface.get(subject))
        object_id = per_row.get(obj, by_surface.get(obj))
        if subject_id is None or object_id is None:
            # No mention of this endpoint anywhere in the book, so there is
            # no entity to hang the edge on. Counted rather than silently
            # lost — `dropped_edges` keeps meaning exactly this.
            report.dropped_edges += 1
            continue
        # `by_surface` is book-global, so an endpoint resolved through it
        # may be absent from this row's own entity_names — but the edge is
        # about to be written against this chunk, and `chunk_entities` is
        # the source of both MENTIONS and LanceDB's `entity_ids`. Leaving
        # it out would hang an edge on a chunk that does not mention its
        # own endpoint, and Phase 3's entity-filtered vector retrieval
        # would fail to return the chunk that evidences the edge. The
        # endpoint is already resolved, so the mention is added rather
        # than the faithfully-extracted edge dropped.
        chunk_entities.setdefault(chunk_id, set()).update((subject_id, object_id))
        raw = str(row.get("predicate") or "")
        rel_statements.append((
            "MATCH (s:Entity {id: $s}), (o:Entity {id: $o})"
            " CREATE (s)-[:REL {predicate: $pred, predicate_raw: $raw,"
            " evidence: $ev, confidence: $conf, chunk_id: $chunk,"
            " book_id: $book, chapter_seq: $seq, run_id: $run}]->(o)",
            {"s": subject_id, "o": object_id,
             "pred": predicate_map.get(raw, raw), "raw": raw,
             "ev": str(row.get("evidence") or ""),
             "conf": float(row.get("confidence") or 0.0),
             "chunk": chunk_id, "book": book_id,
             # 0 means "chunk not in the registry": chapters are 1-based,
             # so no real chapter can be confused with it.
             "seq": int(chapter_of.get(chunk_id, 0)),
             "run": str(row.get("run_id") or "")},
        ))
    report.rel_edges = len(rel_statements)

    statements: list[Statement] = [
        ("MATCH ()-[r:REL]->() WHERE r.book_id = $book DELETE r", {"book": book_id}),
        ("MATCH (c:Chunk)-[m:MENTIONS]->() WHERE c.book_id = $book DELETE m",
         {"book": book_id}),
    ]
    # Sequenced before anything is written back: an orphan is detached and
    # removed outright, a shared entity merely loses this book's claim.
    for node_id in orphan_ids:
        statements.append(
            ("MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": node_id})
        )
    for node_id in sorted(released):
        statements.append((
            "MATCH (e:Entity {id: $id}) SET e.books = $books",
            {"id": node_id, "books": list(released[node_id].books)},
        ))
    statements.append(
        ("MERGE (b:Book {id: $id}) SET b.title = $title, b.author = $author",
         {"id": book_id, "title": title, "author": author})
    )
    for chapter_seq in sorted({chunk.chapter_seq for chunk in chunks}):
        chapter_id = f"{book_id}:{chapter_seq}"
        statements.append((
            "MERGE (c:Chapter {id: $id}) SET c.book_id = $book, c.seq = $seq,"
            " c.title = $title",
            {"id": chapter_id, "book": book_id, "seq": chapter_seq,
             "title": f"Chapter {chapter_seq}"},
        ))
        statements.append((
            "MATCH (b:Book {id: $book}), (c:Chapter {id: $chapter})"
            " MERGE (b)-[:HAS_CHAPTER]->(c)",
            {"book": book_id, "chapter": chapter_id},
        ))
    for chunk in chunks:
        statements.append((
            "MERGE (c:Chunk {id: $id}) SET c.book_id = $book, c.chapter_seq = $seq",
            {"id": chunk.id, "book": book_id, "seq": chunk.chapter_seq},
        ))
        statements.append((
            "MATCH (ch:Chapter {id: $chapter}), (c:Chunk {id: $chunk})"
            " MERGE (ch)-[:HAS_CHUNK]->(c)",
            {"chapter": f"{book_id}:{chunk.chapter_seq}", "chunk": chunk.id},
        ))
    for params in node_writes.values():
        statements.append((
            "MERGE (e:Entity {id: $id}) SET e.name = $name, e.kind = $kind,"
            " e.aliases = $aliases, e.books = $books",
            params,
        ))
    statements.extend(alias_statements)
    statements.extend(rel_statements)
    # MENTIONS only for chunks the registry actually knows: a triplet row
    # naming a chunk_id with no Chunk node would MATCH nothing, writing no
    # edge while still inflating the report.
    for chunk_id in sorted(chunk_entities):
        if chunk_id not in chapter_of:
            continue
        for entity_id_value in sorted(chunk_entities[chunk_id]):
            statements.append((
                "MATCH (c:Chunk {id: $chunk}), (e:Entity {id: $entity})"
                " MERGE (c)-[:MENTIONS]->(e)",
                {"chunk": chunk_id, "entity": entity_id_value},
            ))
            report.mentions += 1

    lance_chunk_rows = [
        {"vector": embeddings[chunk.id], "chunk_id": chunk.id, "book_id": book_id,
         "chapter_seq": chunk.chapter_seq, "text": chunk.text,
         "entity_ids": sorted(chunk_entities.get(chunk.id, set()))}
        for chunk in chunks
        if chunk.id in embeddings
    ]

    # An entity's vector is the centroid of EVERY chunk that mentions it,
    # across every book it belongs to — not just this load's. A shared
    # entity written from only the book in hand would flap between the
    # books' means depending on which loaded last.
    #
    # The other books' contributions are read from the persisted `chunks`
    # rows (authoritative and untouched by this load); this book's come
    # from the rows about to replace its own. That is arithmetically the
    # same as reading the table after the write, but keeps every value
    # computed before phase 2 — nothing here mutates a store.
    entity_rows: dict[str, dict] = {
        target: {"name": params["name"], "kind": params["kind"],
                 "books": params["books"]}
        for target, params in node_writes.items()
    }
    for node_id, node in released.items():
        entity_rows[node_id] = {"name": node.name, "kind": node.kind,
                                "books": list(node.books)}

    other_books = {
        book for params in entity_rows.values() for book in params["books"]
    } - {book_id}
    # (chunk_id, vector) per entity, so the mean is summed in a fixed order
    # and comes out bit-identical whichever book was loaded first.
    per_entity_vectors: dict[str, list[tuple[str, list[float]]]] = {}
    for chunk_id, chunk_entity_ids, vector in lance.chunk_entity_vectors(
        sorted(other_books)
    ):
        for entity_id_value in chunk_entity_ids:
            if entity_id_value in entity_rows:
                per_entity_vectors.setdefault(entity_id_value, []).append(
                    (chunk_id, list(vector))
                )
    for chunk_row in lance_chunk_rows:
        for entity_id_value in chunk_row["entity_ids"]:
            if entity_id_value in entity_rows:
                per_entity_vectors.setdefault(entity_id_value, []).append(
                    (chunk_row["chunk_id"], chunk_row["vector"])
                )

    # An entity no chunk embedding covers falls back to embedding its name.
    uncovered = sorted(
        target for target in entity_rows if target not in per_entity_vectors
    )
    fallback: dict[str, list[float]] = {}
    if uncovered:
        vectors = await embed([entity_rows[t]["name"] for t in uncovered])
        fallback = {
            target: list(vector) for target, vector in zip(uncovered, vectors)
        }
    lance_entity_rows = [
        {"vector": (
            _centroid([v for _cid, v in sorted(per_entity_vectors[target])])
            if target in per_entity_vectors else fallback[target]),
         "entity_id": target,
         "name": params["name"], "kind": params["kind"]}
        for target, params in sorted(entity_rows.items())
    ]

    # ---- Phase 2: the graph, under one transaction.
    _run_in_transaction(kuzu, statements)

    # ---- Phase 3: the vectors. The one window without a transaction.
    try:
        lance.delete_chunks_for_book(book_id)
        if lance_chunk_rows:
            lance.add_chunks(lance_chunk_rows)
        # Orphans go for good; everything being rewritten is replaced.
        lance.delete_entities(orphan_ids + sorted(entity_rows))
        if lance_entity_rows:
            lance.add_entities(lance_entity_rows)
    except Exception as exc:
        raise PartialLoadError(
            f"book {book_id}: the graph was written but the vector store was"
            " left incomplete. Re-run the load to repair it."
        ) from exc
    report.chunks = len(lance_chunk_rows)

    # ---- Phase 4: the questions this load could not answer.
    # Last, and outside both stores: it is a note to the user, not part of
    # the graph. Writing it here rather than in phase 1 means a load that
    # failed leaves the previous run's questions intact, and writing it
    # per book means a re-load REPLACES this book's set — a question that
    # is no longer ambiguous stops costing quota.
    if disambiguation is not None:
        disambiguation.flush(book_id)
    return report
