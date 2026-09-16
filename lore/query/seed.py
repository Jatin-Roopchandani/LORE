"""Question -> the canonical entities a traversal should start from.

The vector index is the entry point, not the answer: LanceDB gives
chunks, each chunk carries the entity ids the loader bridged onto it,
and those ids become traversal seeds.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from lore.extract.embed import QUERY_PREFIX
from lore.query.models import SeedEntity

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]


class _Lance(Protocol):
    def search_chunks(self, vector: list[float], limit: int = 5) -> list[dict]: ...


class _Kuzu(Protocol):
    def execute(self, cypher: str, params: dict | None = None): ...


def canonical_ids(kuzu: _Kuzu, entity_ids: list[str]) -> list[str]:
    """Replace alias stubs with the canonical they point at.

    Spec 2.1: REL edges attach only to canonical nodes. A seed left on an
    alias stub traverses to nothing, which is precisely why Phase 2B's
    multi-hop demo returned zero rows.

    One hop is enough *for this corpus*, not by construction. Measured over
    all 36 ALIAS_OF edges in the loaded graph: 0 chains (no canonical is
    itself an alias), 0 cases where the alias carries more edges than its
    canonical, and 6 pairs where both sides are edgeless. Neither fact is
    enforced by the loader, so if a later load ever produces a chain this
    single hop leaves the seed on a stub silently — it will look like a
    graph that knows nothing, not like an error.
    """
    if not entity_ids:
        return []
    mapping = {
        row[0]: row[1]
        for row in kuzu.execute(
            "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) WHERE a.id IN $ids "
            "RETURN a.id, c.id",
            {"ids": entity_ids},
        )
    }
    seen: list[str] = []
    for entity_id in entity_ids:
        resolved = mapping.get(entity_id, entity_id)
        if resolved not in seen:
            seen.append(resolved)
    return seen


def live_entity_ids(kuzu: _Kuzu, entity_ids: list[str]) -> list[str]:
    """The subset that has at least one REL edge, in the order given.

    A node with degree 0 is not a seed: no template can return a row for it,
    so it can only ever spend a slot. Kuzu answers the aggregate by omitting
    such a node from the result entirely rather than returning a 0 count, so
    membership in the reply is the test. One batched query, not one per seed.
    """
    if not entity_ids:
        return []
    live = {
        row[0]
        for row in kuzu.execute(
            "MATCH (e:Entity)-[r:REL]-() WHERE e.id IN $ids "
            "RETURN e.id, count(r)",
            {"ids": entity_ids},
        )
        if row[1]
    }
    return [i for i in entity_ids if i in live]


async def embed_question(question: str, embed: Embed) -> list[float]:
    """The question's one query vector, with QUERY_PREFIX applied here.

    Pass a raw embedder. Do NOT pass `make_ollama_pair`'s embed: that one
    applies DOCUMENT_PREFIX, and the mismatch degrades retrieval silently
    instead of failing.
    """
    return (await embed([QUERY_PREFIX + question]))[0]


async def seed_entities(
    question: str,
    lance: _Lance,
    kuzu: _Kuzu,
    embed: Embed,
    *,
    k: int = 8,
    max_seeds: int = 8,
    vector: list[float] | None = None,
) -> list[SeedEntity]:
    """The canonical entities the question's vector neighbourhood points at.

    `embed` must apply QUERY_PREFIX -- this function does it, so callers
    pass a raw embedder. Do NOT pass `make_ollama_pair`'s embed: that one
    applies DOCUMENT_PREFIX, and the mismatch degrades retrieval silently
    instead of failing.

    `max_seeds` is 8, not a rounder 6, because 6 measurably excluded the
    subject a question is named after. On the live stores, with degree-0
    entities already dropped, `Mr. Jefferson Hope` is live rank 7 for "Why
    did Jefferson Hope kill Enoch Drebber and Joseph Stangerson?" and
    `Mr. Sherlock Holmes` -- the across_books route to the Sign of the Four
    half of the Scotland Yard question -- is also live rank 7. Hope stays at
    that rank at k=8, 16, 32 and 64, so widening retrieval does not reach
    him; the cap was the wall. Do not "simplify" 8 back to 6.

    `vector`, when given, is the question's vector from `embed_question`,
    and `embed` is not called: the agent embeds once and reuses that vector
    to rank the evidence.
    """
    if vector is None:
        vector = await embed_question(question, embed)
    chunks = lance.search_chunks(vector, limit=k)

    # LanceDB returns a squared-L2 distance, not a similarity. Both sides
    # are unit-normalised nomic vectors (measured: |v| = 1.0000), so the
    # range is [0, 4] and the values seen on this index are 0.5-0.9.
    # Lower is nearer, so a chunk missing `_distance` must fall back to
    # infinity: any finite fallback is a distance some real chunk can
    # exceed -- 1.0 (the obvious guess) is beaten by any pair with cosine
    # below 0.5, which would rank an unscored chunk ahead of a real one.
    best: dict[str, float] = {}
    for chunk in chunks:
        score = float(chunk.get("_distance", float("inf")))
        for entity_id in chunk.get("entity_ids") or []:
            if entity_id not in best or score < best[entity_id]:
                best[entity_id] = score

    if not best:
        return []

    ordered = sorted(best, key=lambda i: (best[i], i))
    # Resolve aliases first, then drop the edgeless, then cap. In that order
    # a dead node never occupies a live slot: on the live corpus `Church`
    # ranked 3rd for Q2 of the acceptance set and `Millbank` 2nd for Q7, and
    # neither can contribute a single row.
    resolved = live_entity_ids(kuzu, canonical_ids(kuzu, ordered))
    rows = {
        row[0]: row
        for row in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids "
            "RETURN e.id, e.name, e.kind, e.books",
            {"ids": resolved},
        )
    }
    seeds: list[SeedEntity] = []
    for entity_id in resolved:
        row = rows.get(entity_id)
        if row is None:
            continue
        seeds.append(
            SeedEntity(
                id=row[0], name=row[1], kind=row[2],
                books=tuple(row[3] or ()),
                score=best.get(entity_id, float("inf")),
            )
        )
    return seeds[:max_seeds]
