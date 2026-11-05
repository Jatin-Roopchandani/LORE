"""What a reader may see: the one visibility predicate behind the reading position.

A reader at `(book, chapter)` of a world has read:
- the chapters up to and including `chapter` of the current book;
- every book EARLIER in the same series (by `series_index`), in full;
- the books whose order against the current book is unknown (another series,
  or no index) ONLY if the reader marked them as read. Order is data, never a
  guess (lore/query/position.py), so such a book never counts on its own;
- never a book known to come later, whatever was marked.

A chunk is therefore visible when its book is fully read, or it is in the
current book and its chapter is at or before the position. `VisibleBooks` is
that predicate. It is a frozenset of the books with anything visible (so every
existing book filter, `x in books`, `sorted(books)`, `r.book_id IN $books`,
keeps working at book level) that also carries `caps`: book id -> the last
visible chapter, for the books read only in part. `sees(books, book_id,
chapter)` is the single test; `book_clause`/`book_params` are the same test as
Cypher over a REL edge or a Chunk node.

Everything that shows the story reads through the scope it is given
(lore/query/scope.py), whose `books` is a `VisibleBooks` while a position is on
and a plain frozenset (or None) otherwise, so with the position off nothing here
changes any output.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from lore.query.models import BookInfo, CodexLine, EdgeRow, Passage, SeedEntity
from lore.query.position import _series_key, order_passages
from lore.reading_position import ReadingPosition


class VisibleBooks(frozenset):
    """The books a reader may read, some only up to a chapter (`caps`)."""

    caps: dict[str, int]

    def __new__(cls, books: Iterable[str] = (), caps: Mapping[str, int] | None = None):
        self = super().__new__(cls, books)
        self.caps = {b: int(c) for b, c in (caps or {}).items() if b in self}
        return self

    def sees(self, book_id: str, chapter: int) -> bool:
        return book_id in self and chapter <= self.caps.get(book_id, math.inf)

    @property
    def capped(self) -> bool:
        return bool(self.caps)


def sees(books: frozenset[str] | None, book_id: str, chapter: int) -> bool:
    """The predicate. `books` None is the whole library; a plain frozenset is
    whole books; a `VisibleBooks` may cap some at a chapter."""
    if books is None:
        return True
    if isinstance(books, VisibleBooks):
        return books.sees(book_id, int(chapter))
    return book_id in books


def capped(books: frozenset[str] | None) -> bool:
    """Whether a reading position limits `books` below whole books."""
    return isinstance(books, VisibleBooks) and books.capped


def restrict(books: frozenset[str] | None, ids: Iterable[str]) -> frozenset[str]:
    """`ids` among `books` (all of `ids` when None), keeping each one's chapter cap."""
    ids = list(ids)
    if books is None:
        return frozenset(ids)
    kept = [b for b in ids if b in books]
    if isinstance(books, VisibleBooks):
        return VisibleBooks(kept, books.caps)
    return frozenset(kept)


def merge(parts: Iterable[frozenset[str]]) -> frozenset[str]:
    """The union of book sets, keeping every chapter cap. Plain when none is capped."""
    parts = list(parts)
    out: set[str] = set().union(*parts) if parts else set()
    caps: dict[str, int] = {}
    for part in parts:
        if isinstance(part, VisibleBooks):
            caps.update(part.caps)
    return VisibleBooks(out, caps) if caps else frozenset(out)


# --- Cypher: the same test over a REL edge or a Chunk node -----------------------------

def cap_clause(books: frozenset[str] | None, alias: str = "r") -> str:
    """Only the chapter caps of `book_clause`: ` AND NOT (alias.book_id = $capb0
    AND alias.chapter_seq > $capc0)` per capped book; "" when nothing is capped."""
    if not capped(books):
        return ""
    return "".join(f" AND NOT ({alias}.book_id = $capb{i} AND {alias}.chapter_seq > $capc{i})"
                   for i, _ in enumerate(sorted(books.caps)))


def cap_params(books: frozenset[str] | None) -> dict:
    """The parameters `cap_clause` binds."""
    if not capped(books):
        return {}
    out: dict = {}
    for i, book in enumerate(sorted(books.caps)):
        out[f"capb{i}"] = book
        out[f"capc{i}"] = books.caps[book]
    return out


def book_clause(books: frozenset[str] | None, alias: str = "r") -> str:
    """` AND alias.book_id IN $books AND NOT (...)` keeping what is visible;
    "" for the whole library. The alias has `book_id` and `chapter_seq`."""
    if books is None:
        return ""
    return f" AND {alias}.book_id IN $books" + cap_clause(books, alias)


def book_params(books: frozenset[str] | None) -> dict:
    """The parameters `book_clause` binds; {} for the whole library."""
    if books is None:
        return {}
    return {"books": sorted(books), **cap_params(books)}


# --- what a position makes visible ---------------------------------------------------------

def _placeable(infos: Mapping[str, BookInfo], book_ids: Iterable[str]) -> dict[str, tuple[str, float]]:
    """Books whose place in a series is known: (series key, index), with an
    index no other of the books has (the rule `order_passages` uses)."""
    by_series: dict[str, list[str]] = {}
    for b in book_ids:
        info = infos.get(b)
        if info and _series_key(info.series) and info.series_index is not None:
            by_series.setdefault(_series_key(info.series), []).append(b)
    out: dict[str, tuple[str, float]] = {}
    for key, members in by_series.items():
        indices = [infos[m].series_index for m in members]
        for m in members:
            if indices.count(infos[m].series_index) == 1:
                out[m] = (key, infos[m].series_index)
    return out


def order_against(infos: Mapping[str, BookInfo], world_books: Iterable[str],
                  current: str) -> dict[str, str]:
    """Each other book of the world against `current`: "before" (earlier in the
    same series), "after" (later in it) or "unknown"."""
    world_books = list(world_books)
    placed = _placeable(infos, world_books)
    out: dict[str, str] = {}
    for b in world_books:
        if b == current:
            continue
        if current in placed and b in placed and placed[b][0] == placed[current][0]:
            out[b] = "before" if placed[b][1] < placed[current][1] else "after"
        else:
            out[b] = "unknown"
    return out


def series_order(infos: Mapping[str, BookInfo], book_ids: Iterable[str]) -> list[str]:
    """The books for a picker: those whose place in a series is known first, by
    series then index, then the others by title. Titles only break ties; they
    never place a book."""
    book_ids = list(book_ids)
    placed = _placeable(infos, book_ids)

    def title(b: str) -> str:
        return (infos.get(b) or BookInfo(title=b)).title.casefold()

    return sorted(book_ids, key=lambda b: ((0, placed[b][0], placed[b][1], title(b), b)
                                           if b in placed else (1, "", 0.0, title(b), b)))


@dataclass(frozen=True)
class PositionView:
    """A position that applies, for the note an answer carries and the badge.

    `chapter` is the stored chapter_seq (the cap); `number` and `heading` are
    the story chapter it falls in, as the reader counts it (front matter is
    not counted; lore/ingest/matter.py), when the caller knows them."""

    book_id: str
    title: str
    chapter: int
    number: int | None = None
    heading: str | None = None

    def place(self) -> str:
        n = self.chapter if self.number is None else self.number
        named = f" ({self.heading})" if self.heading and n else ""
        return f"up to chapter {n}{named} of {self.title}"

    def note(self) -> str:
        return f"Answered from what you've read: {self.place()}"


def position_note(views: Sequence[PositionView]) -> str | None:
    """The line an answer carries when a reading position limited it; None when
    none did. With several worlds each position is named."""
    views = list(views)
    if not views:
        return None
    if len(views) == 1:
        return views[0].note()
    parts = "; ".join(v.place() for v in views)
    return f"Answered from what you've read: {parts}"


def visible_books(world_books: frozenset[str], position: ReadingPosition,
                  infos: Mapping[str, BookInfo]) -> tuple[VisibleBooks, PositionView] | None:
    """What `position` makes visible within the world's books, or None when it
    does not apply (switched off, no book, or a book that is not in the world)."""
    if not position.enabled or not position.book_id or position.book_id not in world_books:
        return None
    relation = order_against(infos, world_books, position.book_id)
    read = {b for b, how in relation.items() if how == "before"}
    read |= {b for b in position.read_books if relation.get(b) == "unknown"}
    info = infos.get(position.book_id) or BookInfo(title=position.book_id)
    caps = {position.book_id: max(0, position.chapter)}
    return (VisibleBooks({position.book_id, *read}, caps),
            PositionView(position.book_id, info.title, max(0, position.chapter)))


# --- entities: who has been met, and under what name ------------------------------------

def _batches(items: Sequence[str], size: int = 500):
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def visible_names(kuzu, books: frozenset[str] | None, ids: Iterable[str],
                  infos: Mapping[str, BookInfo] | None = None) -> dict[str, list[str]]:
    """For canonical entity ids, the names the reader has met them under, the
    earliest first; only ids with something visible are in the result (a visible
    chunk that mentions the entity or one of its alias stubs, or a visible REL
    edge). Without a reading position (`books` not capped) every id is returned
    with an empty list: nothing is limited and names are not renamed.

    The first name is the **earliest visible alias**: an entity merged from
    several surface forms ("a stranger", then a real name) is shown under the
    form the reader met first, never the merged canonical name they may not
    know yet. Ties go to the canonical name."""
    ids = list(dict.fromkeys(ids))
    if not capped(books):
        return {i: [] for i in ids}
    out: dict[str, list[str]] = {}
    for batch in _batches(ids):
        canon = {r[0]: r[1] or "" for r in kuzu.execute(
            "MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id, e.name", {"ids": batch})}
        owner = {i: i for i in canon}
        names = dict(canon)
        for stub, name, target in kuzu.execute(
                "MATCH (a:Entity)-[:ALIAS_OF]->(c:Entity) WHERE c.id IN $ids"
                " RETURN a.id, a.name, c.id", {"ids": batch}):
            owner[stub] = target
            names[stub] = name or ""
        # Canonical nodes first, so a tie in the same chunk keeps the canonical name.
        nodes = sorted(owner, key=lambda n: (n != owner[n], n))
        rows = list(kuzu.execute(
            "MATCH (ch:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.id IN $nodes"
            f"{book_clause(books, 'ch')} RETURN e.id, ch.id, ch.book_id, ch.chapter_seq",
            {"nodes": nodes, **book_params(books)}))
        rows.sort(key=lambda r: (nodes.index(r[0]), r[1]))
        passages = [Passage(n=i, chunk_id=r[1], book_id=r[2], chapter_seq=int(r[3]), text="")
                    for i, r in enumerate(rows)]
        found: dict[str, list[str]] = {}
        for pos in order_passages(passages, infos).ordered:
            node = rows[pos.n][0]
            shown = found.setdefault(owner[node], [])
            if names[node] and names[node] not in shown:
                shown.append(names[node])
        out.update({i: n for i, n in found.items() if n})
        # An entity with a visible relationship but no visible mention is still met.
        silent = [i for i in batch if i in canon and i not in out]
        if silent:
            seen = {r[0] for r in kuzu.execute(
                "MATCH (e:Entity)-[r:REL]-(:Entity) WHERE e.id IN $ids"
                f"{book_clause(books, 'r')} RETURN e.id, count(r)",
                {"ids": silent, **book_params(books)})}
            out.update({i: [canon[i]] for i in silent if i in seen and canon[i]})
    return {i: out[i] for i in ids if i in out}


def tokens(name: str) -> list[str]:
    """The words of a name, lower-cased, for matching a typed name to a shown one."""
    return re.findall(r"\w+", (name or "").casefold())


def met(kuzu, books: frozenset[str] | None, ids: Iterable[str],
        infos: Mapping[str, BookInfo] | None = None) -> set[str]:
    """The ids (canonical) the reader has met; every id without a position."""
    return set(visible_names(kuzu, books, ids, infos))


def display_name(names: Mapping[str, list[str]], entity_id: str, canonical: str) -> str:
    """The name to show: the earliest visible alias, else the canonical name."""
    found = names.get(entity_id)
    return found[0] if found else canonical


def shown_seeds(kuzu, books: frozenset[str] | None, seeds: Sequence[SeedEntity],
                infos: Mapping[str, BookInfo] | None) -> list[SeedEntity]:
    """Under a reading position, the seeds named by the earliest alias the reader
    has met and listing only the books they may read; unchanged without one."""
    if not capped(books) or not seeds:
        return list(seeds)
    names = visible_names(kuzu, books, [s.id for s in seeds], infos)
    return [replace(s, name=display_name(names, s.id, s.name),
                    books=tuple(b for b in s.books if b in books)) for s in seeds]


def shown_rows(kuzu, books: frozenset[str] | None, rows: list[EdgeRow],
               infos: Mapping[str, BookInfo] | None) -> list[EdgeRow]:
    """Under a reading position, the relationship rows with their end names given
    as the earliest alias the reader has met; unchanged without one."""
    if not capped(books) or not rows:
        return rows
    ids = {i for r in rows for i in (r.subject_id, r.object_id) if i}
    names = visible_names(kuzu, books, ids, infos)
    return [replace(r, subject=display_name(names, r.subject_id, r.subject),
                    object=display_name(names, r.object_id, r.object)) for r in rows]


def respell(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """`text` with each unmet name written as the name the reader has met
    (whole words, any case); the longest names first."""
    for old, new in sorted(pairs, key=lambda p: -len(p[0])):
        if old and new and old.casefold() != new.casefold():
            text = re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", lambda _m, n=new: n, text,
                          flags=re.IGNORECASE)
    return text


def shown_beats(kuzu, books: frozenset[str] | None, beats: Sequence,
                infos: Mapping[str, BookInfo] | None) -> list:
    """Under a reading position, each beat's entity names (and its text) given
    as the names the reader has met: a beat was written over the whole story and
    may use a merged name the reader does not know yet. Unchanged without one."""
    if not capped(books) or not beats:
        return list(beats)
    names = visible_names(kuzu, books, {i for b in beats for i in b.entity_ids if i}, infos)
    out = []
    for b in beats:
        pairs, entities = [], []
        for name, entity_id in zip(b.entities, b.entity_ids or ("",) * len(b.entities)):
            met_as = names.get(entity_id)
            if met_as and name.casefold() not in {m.casefold() for m in met_as}:
                pairs.append((name, met_as[0]))
                name = met_as[0]
            entities.append(name)
        out.append(replace(b, entities=tuple(entities), text=respell(b.text, pairs)) if pairs else b)
    return out


def shown_lines(kuzu, books: frozenset[str] | None, lines: Sequence[CodexLine],
                infos: Mapping[str, BookInfo] | None) -> list[CodexLine]:
    """Under a reading position, codex lines with their subject (and text) given
    as the name the reader has met. Unchanged without one."""
    if not capped(books) or not lines:
        return list(lines)
    names = visible_names(kuzu, books, {l.entity_id for l in lines if l.entity_id}, infos)
    out = []
    for l in lines:
        met_as = names.get(l.entity_id)
        if met_as and l.subject.casefold() not in {m.casefold() for m in met_as}:
            out.append(replace(l, subject=met_as[0], text=respell(l.text, [(l.subject, met_as[0])])))
        else:
            out.append(l)
    return out
