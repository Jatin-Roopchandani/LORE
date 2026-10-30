"""Candidates for a lens about a set of people that names none.

An interpretive question such as a comparison or an evaluation ("who is the
most ...") often has a lens about people in general, and nothing in it names
the people to weigh, so retrieval started from near-random minor characters.
For such a lens (`wants_candidates`: it names no entity of its own, and the
entities nearest to it are mostly characters) the agent:
  - drops whoever the question excludes ("except X and Y", `exclusions`,
    `is_excluded`);
  - adds candidates from the graph (`candidate_seeds`): the characters whose
    mentions span the most chapters of the part's books, re-ranked by beat
    variety (`rank_by_variety`): characters with many kinds of moment, in
    many chapters, come before characters who are merely present;
  - gives each candidate a few beats spread over the story
    (timeline.candidate_timeline).

This module no longer decides whether a question asks for a judgement:
decompose.py classifies each part, and the interpretive answer (interpret.py)
replaces the old judgement sentence with a reading. Nothing here looks at the
wording of a question except the exclusion phrases, and nothing refers to any
library.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from lore.beats.store import beats_for_entities
from lore.query.models import SeedEntity
from lore.query.seed import NameIndex, canonical_ids, named_entity_ids, named_seeds
from lore.resolve.normalize import normalize_name

# Candidates gathered for a lens, and the number of named characters at or
# above which the question has already chosen its people.
JUDGE_CANDIDATES = 5
FEW_NAMED = 2
# Rows read before exclusions and book-title nodes are removed.
_OVERFETCH = 40
# Candidates ranked by chapter span are taken this many times over before beat
# variety picks the final ones.
VARIETY_POOL = 3

# --- exclusions -------------------------------------------------------------

_EXCEPT = re.compile(
    r"\b(?:except(?:\s+for)?|excluding|other\s+than|apart\s+from|aside\s+from|besides|"
    r"not\s+counting|leaving\s+out|without\s+counting|but\s+not)\s+(?P<tail>[^?.;:!]+)", re.I)
# Where the list of names stops and the rest of the question resumes.
_TAIL_END = re.compile(
    r"\s+(?:in|from|among|across|within|for|when|because|as|who|which|that|if|since|during|"
    r"throughout|and\s+(?:why|how|what|who|which))\b", re.I)
_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b|\bor\b|\bnor\b)\s*", re.I)


def exclusions(text: str) -> list[str]:
    """The names a question rules out ("except X and Y", "other than X, Y or
    Z", "excluding X"), in order, without repeats."""
    out: list[str] = []
    for match in _EXCEPT.finditer(text or ""):
        tail = _TAIL_END.split(match.group("tail"), maxsplit=1)[0]
        for piece in _SPLIT.split(tail):
            name = re.sub(r"^(?:the|both|either|neither)\s+", "", piece.strip(), flags=re.I).strip(" \"'")
            if name and re.search(r"\w", name) and name.casefold() not in {o.casefold() for o in out}:
                out.append(name)
    return out


def _tokens(name: str) -> frozenset[str]:
    return frozenset(normalize_name(str(name)).split())


def is_excluded(name: str, aliases: Iterable[str], phrases: Sequence[str]) -> bool:
    """Whether the entity (its name or any alias) is one of the excluded
    `phrases`: equal, or all the words of one inside the other ("Zed" rules
    out "Zed Quill", and "Zed Quill" rules out a node named "Zed")."""
    if not phrases:
        return False
    wanted = [t for t in (_tokens(p) for p in phrases) if t]
    for form in (name, *(aliases or ())):
        have = _tokens(form)
        if have and any(have <= want or want <= have for want in wanted):
            return True
    return False


# --- candidates -------------------------------------------------------------

_BOOKS_CLAUSE = " AND c.book_id IN $books"
_CANDIDATES = (
    "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.kind = 'Character'{books} "
    "WITH e, c.book_id AS book, c.chapter_seq AS chapter, count(c) AS n "
    "RETURN e.id, e.name, e.aliases, count(*) AS chapters, sum(n) AS mentions "
    "ORDER BY chapters DESC, mentions DESC, e.name LIMIT $limit")


def beat_variety(conn, entity_id: str) -> tuple[int, int]:
    """(distinct beat kinds, distinct (book, chapter) places) over the entity's
    beats: a character with an arc has many of both."""
    beats = beats_for_entities(conn, [entity_id])
    return (len({b.kind for b in beats}), len({(b.book_id, b.chapter_seq) for b in beats}))


def rank_by_variety(conn, seeds: Sequence[SeedEntity]) -> list[SeedEntity]:
    """The seeds with the most varied beats first (kinds, then chapters); ties
    keep their given order. Without beats, or with no usable `conn`, the given
    order: this is a refinement, never a reason to fail the question."""
    if conn is None or len(seeds) < 2:
        return list(seeds)
    try:
        scores = {s.id: beat_variety(conn, s.id) for s in seeds}
    except sqlite3.Error:
        return list(seeds)
    return sorted(seeds, key=lambda s: scores[s.id], reverse=True)


async def candidate_seeds(kuzu, book_ids: Sequence[str], phrases: Sequence[str],
                          exclude_names: frozenset[str], *,
                          limit: int = JUDGE_CANDIDATES, conn=None) -> list[SeedEntity]:
    """The `limit` Character entities that span the most of the story, most
    first: distinct (book, chapter) places whose chunks mention them, then
    the number of mentions. With `conn`, the top `limit` x VARIETY_POOL by that
    are re-ranked by beat variety (`rank_by_variety`) and the best `limit` kept.
    Restricted to `book_ids` when given. Anyone the question excludes
    (`phrases`, matched on name and aliases) and any node named like a book
    title is skipped. A graph that cannot answer gives []: this is an extra,
    never a reason to fail the question."""
    params: dict = {"limit": max(limit, 1) + _OVERFETCH}
    if book_ids:
        params["books"] = list(book_ids)
    try:
        rows = list(kuzu.execute(_CANDIDATES.format(books=_BOOKS_CLAUSE if book_ids else ""), params))
        ids = [str(r[0]) for r in rows
               if normalize_name(str(r[1] or "")) not in exclude_names
               and not is_excluded(str(r[1] or ""), r[2] or (), phrases)]
        seeds = await named_seeds(kuzu, canonical_ids(kuzu, ids), exclude_names=exclude_names)
    except Exception:  # noqa: BLE001 -- see the docstring
        return []
    if conn is not None:
        return rank_by_variety(conn, seeds[:max(limit, 1) * VARIETY_POOL])[:limit]
    return seeds[:limit]


def wants_candidates(seeds: Sequence[SeedEntity], named: Sequence[SeedEntity]) -> bool:
    """Whether a lens is about a set of people and names none: no entity of its
    own is named (in the lens or the question) and at least half of the
    entities nearest to it are characters. A structural test, never about the
    wording of a question."""
    return (not named and bool(seeds)
            and 2 * sum(s.kind == "Character" for s in seeds) >= len(seeds))


@dataclass
class JudgeSeeds:
    """A judgement part's seeds. `seeds`: the named characters first, then the
    candidates, then the part's other seeds; `candidates`: the graph's picks;
    `named`: the question's own (non-excluded) named seeds."""

    seeds: list[SeedEntity]
    candidates: list[SeedEntity] = field(default_factory=list)
    named: list[SeedEntity] = field(default_factory=list)


async def judge_seeds(kuzu, names: NameIndex, part: str, question: str,
                      seeds: Sequence[SeedEntity], named: Sequence[SeedEntity],
                      book_ids: Sequence[str], exclude_names: frozenset[str], *,
                      limit: int = JUDGE_CANDIDATES, conn=None) -> JudgeSeeds:
    """The seeds of a lens about a set of people: whoever the question (or the
    lens, if it was rewritten) excludes is removed; with fewer than FEW_NAMED
    named characters left, the top candidates of the part's books are added
    (re-ranked by beat variety when `conn` is given)."""
    phrases = list(dict.fromkeys([*exclusions(part), *exclusions(question)]))
    gone: set[str] = set()
    for phrase in phrases:
        gone.update(canonical_ids(kuzu, named_entity_ids(phrase, names)))

    def kept(seed: SeedEntity) -> bool:
        return seed.id not in gone and not is_excluded(seed.name, (), phrases)

    named_left = [s for s in named if kept(s)]
    others = [s for s in seeds if kept(s) and s.id not in {n.id for n in named_left}]
    candidates: list[SeedEntity] = []
    if sum(s.kind == "Character" for s in named_left) < FEW_NAMED:
        candidates = [s for s in await candidate_seeds(kuzu, book_ids, phrases, exclude_names, limit=limit, conn=conn)
                      if s.id not in {n.id for n in named_left}]
    ordered = list(dict.fromkeys(s.id for s in [*named_left, *candidates, *others]))
    by_id = {s.id: s for s in [*others, *candidates, *named_left]}
    return JudgeSeeds(seeds=[by_id[i] for i in ordered], candidates=candidates, named=named_left)
