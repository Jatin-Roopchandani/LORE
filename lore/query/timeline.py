"""The query timeline: a character's, faction's or object's story beats from
across the whole story, chosen for one part of a question.

A question about an arc, a change or a relationship needs moments from the
whole story, and those rarely match the question's wording, so passage
search alone misses them. Beats (lore/beats) are quote-anchored moments per
entity. For each part of a question the agent takes the beats of that
part's seed entities, picks up to 24 of them (an even spread across the
story plus the most relevant to the part), and adds their passages to the
evidence and a short "Story timeline" to the claims prompt. Beats only
widen what the claims step sees: claims still quote numbered passages and
are still checked, so a beat is never a claim.

Off (`TIMELINE = False`), with no beats, or with no timeline entity in a
part, nothing here changes anything.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping, Sequence

from lore.beats.store import Beat, beats_for_entities
from lore.query.models import BookInfo, Passage, SeedEntity, TimelineBeat
from lore.query.position import order_passages

TIMELINE = True
# The share of the evidence budget set aside for timeline passages. Ranked
# evidence keeps the rest, and whatever the timeline does not use.
TIMELINE_SHARE = 0.30
# Entity kinds that have a timeline (places do not), and how many entities
# one part of a question may bring.
TIMELINE_KINDS = ("Character", "Faction", "Object")
ENTITIES_PER_PART = 3
# A part's beats are shown whole up to SPREAD + RELEVANT; past that, SPREAD of
# them evenly across the story plus the RELEVANT most like the part.
SPREAD = 12
RELEVANT = 12


def timeline_entities(seeds: Iterable[SeedEntity],
                      kinds: Sequence[str] = TIMELINE_KINDS,
                      limit: int = ENTITIES_PER_PART) -> list[SeedEntity]:
    """The part's seeds that can have a timeline, in seed order, at most
    `limit`. The seeds are already free of book-title nodes."""
    return [s for s in seeds if s.kind in kinds][:limit]


def order_beats(beats: Sequence[Beat],
                books: Mapping[str, BookInfo] | None) -> list[Beat]:
    """The beats in story order: `position.order_passages` semantics, so two
    books are ordered against each other only when their series says how."""
    passages = [Passage(n=i, chunk_id=b.chunk_id, book_id=b.book_id,
                        chapter_seq=b.chapter_seq, text="")
                for i, b in enumerate(beats)]
    return [beats[pos.n] for pos in order_passages(passages, books).ordered]


def coverage_order(n: int) -> list[int]:
    """Indices 0..n-1 so that any prefix covers the span evenly: both ends
    first, then midpoints, breadth first. A budget that cuts the list still
    holds the earliest and latest moments and an even sample between."""
    if n <= 0:
        return []
    out = [0] if n == 1 else [0, n - 1]
    pending = [(0, n - 1)]
    while pending:
        lo, hi = pending.pop(0)
        mid = (lo + hi) // 2
        if mid in (lo, hi):
            continue
        out.append(mid)
        pending += [(lo, mid), (mid, hi)]
    return out


def select_beats(beats: Sequence[Beat], part_vector: list[float], lance,
                 books: Mapping[str, BookInfo] | None) -> list[Beat]:
    """At most SPREAD + RELEVANT beats, in story order. With that many or
    fewer, all of them. Otherwise SPREAD beats evenly spaced across the
    ordered list (always the earliest and the latest), plus the RELEVANT
    beats most similar to the part among the others."""
    ordered = order_beats(beats, books)
    if len(ordered) <= SPREAD + RELEVANT:
        return ordered
    last = len(ordered) - 1
    spread = sorted({round(i * last / (SPREAD - 1)) for i in range(SPREAD)})
    chosen = {ordered[i].id for i in spread}
    rest = [b.id for b in ordered if b.id not in chosen]
    for row in lance.search_beats(part_vector, rest, RELEVANT):
        chosen.add(str(row["id"]))
    return [b for b in ordered if b.id in chosen]


def part_timeline(conn: sqlite3.Connection, lance, seeds: Iterable[SeedEntity],
                  part_vector: list[float],
                  books: Mapping[str, BookInfo] | None,
                  book_ids: Sequence[str] | None = None) -> list[Beat]:
    """The selected beats for one part of a question; [] when it has no
    timeline entity or they have no beats. A part that names books keeps
    only those books' beats, as its passages are scoped to them
    (agent.SCOPE_PENALTY)."""
    entities = timeline_entities(seeds)
    if not entities:
        return []
    try:
        beats = beats_for_entities(conn, [e.id for e in entities])
    except sqlite3.OperationalError:  # a library not migrated to beats yet
        return []
    if book_ids:
        beats = [b for b in beats if b.book_id in book_ids]
    if not beats:
        return []
    return select_beats(beats, part_vector, lance, books)


# A judgement question (judge.py) weighs several candidates, so each brings a
# few beats spread over the story, and together they stay under one cap.
BEATS_PER_CANDIDATE = 3
MIN_BEATS_PER_CANDIDATE = 2
MAX_CANDIDATE_BEATS = 12


def candidate_timeline(conn: sqlite3.Connection, candidates: Sequence[SeedEntity],
                       books: Mapping[str, BookInfo] | None,
                       book_ids: Sequence[str] | None = None) -> list[Beat]:
    """A few beats of each candidate, in story order: the earliest, the latest
    and one between (two each when many candidates share the cap). A part that
    names books keeps only those books' beats. [] when there are no
    candidates, no beats, or the library has no beats tables yet."""
    if not candidates:
        return []
    try:
        beats = beats_for_entities(conn, [c.id for c in candidates])
    except sqlite3.OperationalError:  # a library not migrated to beats yet
        return []
    if book_ids:
        beats = [b for b in beats if b.book_id in book_ids]
    each = max(MIN_BEATS_PER_CANDIDATE,
               min(BEATS_PER_CANDIDATE, MAX_CANDIDATE_BEATS // len(candidates)))
    chosen: dict[str, Beat] = {}
    for candidate in candidates:
        mine = [b for b in beats if candidate.id in b.entity_ids]
        # Beats that are about the candidate come before beats that only mention them.
        about = [b for b in mine if b.entity_ids and b.entity_ids[0] == candidate.id]
        ordered = order_beats(about if len(about) >= each else mine, books)
        for i in sorted(coverage_order(len(ordered))[:each]):
            chosen.setdefault(ordered[i].id, ordered[i])
    picked = order_beats(list(chosen.values()), books)
    if len(picked) <= MAX_CANDIDATE_BEATS:
        return picked
    # Over the cap (many candidates, two each): the cap's own even spread.
    return [picked[i] for i in sorted(coverage_order(len(picked))[:MAX_CANDIDATE_BEATS])]


def judgement_timeline(conn: sqlite3.Connection, lance, named: Sequence[SeedEntity],
                       candidates: Sequence[SeedEntity], part_vector: list[float],
                       books: Mapping[str, BookInfo] | None,
                       book_ids: Sequence[str] | None = None) -> list[Beat]:
    """A judgement part's beats: the question's own named characters get the
    ordinary timeline, the graph's candidates a few spread beats each. Other
    seeds (found by vector search, so possibly minor) bring none."""
    beats = {b.id: b for b in part_timeline(conn, lance, named, part_vector, books, book_ids)}
    for b in candidate_timeline(conn, candidates, books, book_ids):
        beats.setdefault(b.id, b)
    return order_beats(list(beats.values()), books)


def timeline_beats(beats: Iterable[Beat]) -> tuple[TimelineBeat, ...]:
    """Beats as the lines the evidence and the prompt carry."""
    return tuple(TimelineBeat(chunk_id=b.chunk_id, book_id=b.book_id,
                              chapter_seq=b.chapter_seq,
                              subject=b.entities[0] if b.entities else "",
                              text=b.text) for b in beats)
