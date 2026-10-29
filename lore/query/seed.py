"""Question -> the canonical entities a traversal should start from.

The vector index is the entry point, not the answer: LanceDB gives
chunks, each chunk carries the entity ids the loader bridged onto it,
and those ids become traversal seeds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Collection, Iterable, Protocol, Sequence

from lore.extract.embed import QUERY_PREFIX
from lore.query.models import SeedEntity
from lore.query.retrieval import search_chunks
from lore.resolve.normalize import normalize_name

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
    named: Sequence[SeedEntity] = (),
    exclude_names: frozenset[str] = frozenset(),
    book_ids: Collection[str] | None = None,
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

    `exclude_names` are normalised library book titles: an entity made from
    a book's own title is no seed (see `_entities`).

    `named` (from `named_seeds`) come first and are not counted against
    `max_seeds`; a vector seed that is also named appears once, as named.

    `book_ids`, when given (a world's books), confine the chunks the seeds
    are read from and the entities returned to those books.
    """
    if vector is None:
        vector = await embed_question(question, embed)
    # Story chunks only: a licence or contents chunk bridges to junk entities.
    chunks = search_chunks(lance, vector, k, book_ids=book_ids)

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
        return list(named)

    ordered = sorted(best, key=lambda i: (best[i], i))
    # Resolve aliases first, then drop the edgeless, then cap. In that order
    # a dead node never occupies a live slot: on the live corpus `Church`
    # ranked 3rd for Q2 of the acceptance set and `Millbank` 2nd for Q7, and
    # neither can contribute a single row.
    resolved = live_entity_ids(kuzu, canonical_ids(kuzu, ordered))
    # Named seeds do not spend the vector cap (Task 10d): John Ferrier, named
    # in Q3, sat at live rank 9 and was cut.
    taken = {seed.id for seed in named}
    vector_seeds = _entities(kuzu, [i for i in resolved if i not in taken], best,
                             exclude_names, book_ids)
    return [*named, *vector_seeds[:max_seeds]]


def _entities(kuzu: _Kuzu, entity_ids: list[str], best: dict[str, float],
              exclude_names: frozenset[str] = frozenset(),
              book_ids: Collection[str] | None = None) -> list[SeedEntity]:
    """SeedEntity per id, in the order given; an id with no node is skipped,
    and so is one whose normalised name is in `exclude_names` -- a node made
    from a book's own title ("A Study in Scarlet", Object) is a junk seed:
    the title is mentioned in prose, but it is no character or object a
    traversal can learn anything from. With `book_ids`, an entity none of
    whose books is among them is skipped too."""
    if not entity_ids:
        return []
    rows = {
        row[0]: row
        for row in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids "
            "RETURN e.id, e.name, e.kind, e.books",
            {"ids": entity_ids},
        )
    }
    return [
        SeedEntity(id=row[0], name=row[1], kind=row[2], books=tuple(row[3] or ()),
                   score=best.get(entity_id, float("inf")))
        for entity_id in entity_ids
        if (row := rows.get(entity_id)) is not None
        and normalize_name(str(row[1] or "")) not in exclude_names
        and (book_ids is None or any(b in book_ids for b in row[3] or ()))
    ]


# --- Task 10d: entities the question names outright ---------------------------

_TOKEN = re.compile(r"\w+")
# Stripped from the front of a name before it is indexed, so "The Aurora"
# is also found as "Aurora". Never a name on their own.
_ARTICLES = frozenset({"the", "a", "an"})
# Titles and forms of address. A name made only of these ("Mr.", "the
# Doctor") would match every question that uses the word, so it is not
# indexed; with a real name beside it ("Mr. Fox") it is.
_TITLES = frozenset({
    "mr", "mrs", "miss", "ms", "master", "dr", "doctor", "sir", "madam", "lady",
    "lord", "captain", "major", "colonel", "general", "lieutenant", "sergeant",
    "inspector", "constable", "professor", "rev", "reverend", "father", "mother",
    "brother", "sister", "uncle", "aunt", "young", "old", "st", "saint", "king",
    "queen", "prince", "princess", "of",
})
MIN_ONE_WORD_NAME = 4


def _qualifies(tokens: tuple[str, ...]) -> bool:
    """A phrase is a usable name: some word in it is not a title or article,
    and it is either several words or one word of MIN_ONE_WORD_NAME+ letters."""
    core = tuple(t for t in tokens if t not in _ARTICLES)
    if not any(t not in _TITLES for t in core):
        return False
    return len(core) >= 2 or len(core[0]) >= MIN_ONE_WORD_NAME


@dataclass(frozen=True)
class NameIndex:
    """Lower-cased word sequence -> the entity ids with that name or alias."""

    phrases: dict[tuple[str, ...], tuple[str, ...]]
    longest: int

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence]) -> "NameIndex":
        """`rows` are (entity id, name, aliases)."""
        phrases: dict[tuple[str, ...], list[str]] = {}
        for entity_id, name, aliases in rows:
            for text in (name, *(aliases or ())):
                tokens = tuple(_TOKEN.findall(str(text or "").lower()))
                forms = {tokens}
                while tokens and tokens[0] in _ARTICLES:
                    tokens = tokens[1:]
                    forms.add(tokens)
                for form in forms:
                    if form and _qualifies(form):
                        ids = phrases.setdefault(form, [])
                        if entity_id not in ids:
                            ids.append(entity_id)
        return cls(phrases={k: tuple(sorted(v)) for k, v in phrases.items()},
                   longest=max((len(k) for k in phrases), default=0))


def load_name_index(kuzu: _Kuzu, book_ids: Collection[str] | None = None) -> NameIndex:
    """Every entity's name and aliases, in ONE read. Call once per question.
    With `book_ids` (a world's books) only entities in those books are
    indexed, so a name never finds an entity of another world."""
    if book_ids is None:
        return NameIndex.from_rows(
            (row[0], row[1], row[2])
            for row in kuzu.execute("MATCH (e:Entity) RETURN e.id, e.name, e.aliases")
        )
    return NameIndex.from_rows(
        (row[0], row[1], row[2])
        for row in kuzu.execute(
            "MATCH (e:Entity) RETURN e.id, e.name, e.aliases, e.books")
        if any(b in book_ids for b in row[3] or ())
    )


def named_entity_ids(text: str, index: NameIndex) -> list[str]:
    """Entity ids whose name or alias appears in `text` as a whole phrase.

    Case-insensitive, on word boundaries, in order of appearance. A match
    lying inside a longer match is not a second match: "Jefferson Hope"
    does not also name the node "Jefferson". One exception to
    case-insensitivity: in a question written with capitals, a lower-case
    single word is ordinary prose ("a small boat"), not the name "Small".
    In an all-lower-case question casing says nothing, so it still counts.
    """
    words = list(_TOKEN.finditer(text))
    lowered = [w.group().lower() for w in words]
    cased = any(ch.isupper() for ch in text)
    spans: list[tuple[int, int, tuple[str, ...]]] = []
    for size in range(min(index.longest, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            ids = index.phrases.get(tuple(lowered[start:start + size]))
            if not ids:
                continue
            if size == 1 and cased and words[start].group().islower():
                continue
            if any(a <= start and start + size <= b for a, b, _ in spans):
                continue
            spans.append((start, start + size, ids))
    found: list[str] = []
    for _, _, ids in sorted(spans):
        for entity_id in ids:
            if entity_id not in found:
                found.append(entity_id)
    return found


async def named_seeds(kuzu: _Kuzu, entity_ids: Sequence[str], *,
                      exclude_names: frozenset[str] = frozenset(),
                      book_ids: Collection[str] | None = None) -> list[SeedEntity]:
    """Named entity ids as seeds: resolved to their canonical, edgeless dropped,
    in the order given. Uncapped; the caller's vector seeds carry the cap."""
    resolved = live_entity_ids(kuzu, canonical_ids(kuzu, list(entity_ids)))
    return _entities(kuzu, resolved, {i: 0.0 for i in resolved}, exclude_names, book_ids)
