"""Story position: where each passage sits in its book and series.

Order is data, never a guess. A passage's position is its book's place in a
series (when known), its chapter, and its chunk's number within the chapter
(the last part of a chunk id, `book:chapter:seq`). Two books are ordered
against each other ONLY when both belong to the same series and both have
an index in it; otherwise their relative order is unknown, and is said to
be unknown. Nothing here looks at creation dates, publication dates, ids or
titles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from lore.query.models import BookInfo, Passage


@dataclass(frozen=True)
class Position:
    n: int  # the passage number
    book_id: str
    title: str
    series: str | None  # set only when the book's order within it is known
    series_index: float | None
    chapter: int
    seq: int

    def label(self, detail: bool = False) -> str:
        """"Title, book 2 of Series, chapter 5"; `detail` adds the chunk's
        place in the chapter, so two facts of one chapter can be ordered."""
        parts = [self.title]
        if self.series is not None and self.series_index is not None:
            parts.append(f"book {self.series_index:g} of {self.series}")
        parts.append(f"chapter {self.chapter}")
        if detail:
            parts.append(f"passage {self.seq + 1} in the chapter")
        return ", ".join(parts)


@dataclass(frozen=True)
class StoryOrder:
    ordered: list[Position]  # story order, as far as it is known
    positions: dict[int, Position]  # by passage number
    # Sets of book titles whose order against each other is unknown: each
    # entry is one run of books known to be in order (a single title when
    # the book stands alone). Empty when every pair of books is ordered.
    units: list[list[str]] = field(default_factory=list)

    def note(self) -> str:
        """One line saying which books' relative order is unknown; "" when
        there is nothing unknown to say."""
        if len(self.units) < 2:
            return ""
        shown = "; ".join(", ".join(u) + (" (in that order)" if len(u) > 1 else "")
                          for u in self.units)
        return f"Relative story order is unknown between: {shown}."


def _series_key(series: str | None) -> str:
    return " ".join((series or "").split()).casefold()


def _seq(chunk_id: str) -> int:
    try:
        return int(chunk_id.rsplit(":", 1)[-1])
    except ValueError:
        return 0


def order_passages(passages: Iterable[Passage],
                   books: Mapping[str, BookInfo] | None) -> StoryOrder:
    """The passages in story order, each with its position label.

    Books of one series with distinct indices form one run, ordered by
    index; every other book stands alone. Runs and lone books are listed in
    a fixed arrangement (runs by series name, then lone books as first
    met) that implies no order between them -- `note()` says so.
    """
    passages = list(passages)
    books = books or {}
    present: list[str] = []
    for passage in passages:
        if passage.book_id not in present:
            present.append(passage.book_id)

    def info(book_id: str) -> BookInfo:
        return books.get(book_id) or BookInfo(title=book_id)

    # Which books can be placed in a series run: same series, an index each,
    # and an index no other book of that series has.
    by_series: dict[str, list[str]] = {}
    for book_id in present:
        b = info(book_id)
        if _series_key(b.series) and b.series_index is not None:
            by_series.setdefault(_series_key(b.series), []).append(book_id)
    runs: list[list[str]] = []
    placed: set[str] = set()
    for key in sorted(by_series):
        members = by_series[key]
        indices = [info(m).series_index for m in members]
        clean = [m for m in members if indices.count(info(m).series_index) == 1]
        if clean:
            runs.append(sorted(clean, key=lambda m: info(m).series_index))
            placed.update(clean)
    units = runs + [[b] for b in present if b not in placed]
    rank = {b: i for i, unit in enumerate(units) for b in unit}
    position_in_run = {b: i for unit in units for i, b in enumerate(unit)}

    positions = {}
    for passage in passages:
        b = info(passage.book_id)
        usable = passage.book_id in placed
        positions[passage.n] = Position(
            n=passage.n, book_id=passage.book_id, title=b.title,
            series=b.series.strip() if usable and b.series else None,
            series_index=b.series_index if usable else None,
            chapter=passage.chapter_seq, seq=_seq(passage.chunk_id))
    ordered = sorted(positions.values(), key=lambda q: (
        rank[q.book_id], position_in_run[q.book_id], q.chapter, q.seq, q.n))
    return StoryOrder(ordered=ordered, positions=positions,
                      units=[[info(b).title for b in unit] for unit in units])
