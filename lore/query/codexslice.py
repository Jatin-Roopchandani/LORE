"""The query codex slice: what the codex holds about a part's entities.

A codex entry (lore/codex) is a quote-verified profile of one entity. For a
part of a question the agent takes the entries of the part's seed entities,
picks up to CODEX_PER_PART of their statements (an even spread across the
story plus the most relevant to the part, as the timeline does for beats), and
adds their passages to the evidence and a compact "Codex" block to the claims
prompt (synthesize._codex_block). The codex only widens what the claims step
sees: claims still quote numbered passages and are still checked, so a codex
line is never a claim.

Gated by `decompose.DEEP`. With it off, with no codex rows, or with no seed
that has an entry, nothing here changes anything.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping, Sequence

from lore.codex import store as codex_store
from lore.query.models import BookInfo, CodexLine, Passage, SeedEntity
from lore.query.position import order_passages
from lore.query.timeline import coverage_order

# The share of the evidence budget set aside for codex passages (the part of the
# budget only they fill; what they do not use goes back to ranked passages).
CODEX_SHARE = 0.20
# Entities one focused part may bring, and the statements a part's slice holds:
# SPREAD evenly across the story plus the RELEVANT most like the part.
ENTITIES_PER_PART = 3
SPREAD = 8
RELEVANT = 8
# The lines the claims prompt shows (the evidence cap decides which are citable).
BLOCK_LINES = 40


def active() -> bool:
    from lore.query import decompose
    return decompose.DEEP


def _positions(conn: sqlite3.Connection, chunk_ids: Sequence[str]) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    ids = sorted(set(chunk_ids))
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        for cid, book, chapter in conn.execute(
                "SELECT id, book_id, chapter_seq FROM chunks WHERE id IN"
                f" ({','.join('?' * len(batch))})", batch):
            out[cid] = (book, int(chapter))
    return out


def lines_for(conn: sqlite3.Connection, entity_ids: Sequence[str], *,
              book_ids: Iterable[str] | None = None,
              aspects: Mapping[str, set[str]] | None = None) -> list[CodexLine]:
    """Every codex statement of these entities as a line, entity by entity, in
    saved order. `book_ids` keeps the statements whose passage is in one of
    those books; `aspects` (entity id -> aspect names) keeps only those aspects
    of that entity. [] when the library has no codex tables yet."""
    try:
        entries = codex_store.entries_for(conn, entity_ids)
    except sqlite3.OperationalError:  # a library not migrated to the codex yet
        return []
    where = _positions(conn, [s.chunk_id for e in entries for s in e.statements])
    books = None if book_ids is None else set(book_ids)
    out = []
    for entry in entries:
        only = (aspects or {}).get(entry.entity_id)
        for s in entry.statements:
            place = where.get(s.chunk_id)
            if place is None or (books and place[0] not in books):
                continue
            if only is not None and s.aspect not in only:
                continue
            out.append(CodexLine(chunk_id=s.chunk_id, book_id=place[0],
                                 chapter_seq=place[1], subject=entry.name,
                                 aspect=s.aspect, text=s.text))
    return out


def _ids_of(conn: sqlite3.Connection, entity_ids: Sequence[str]) -> dict[tuple, str]:
    """(entity name, statement text, chunk id) -> statement id."""
    out = {}
    for entry in codex_store.entries_for(conn, entity_ids):
        for s in entry.statements:
            out[(entry.name, s.text, s.chunk_id)] = s.id
    return out


def order_lines(lines: Sequence[CodexLine],
                books: Mapping[str, BookInfo] | None) -> list[CodexLine]:
    """Story order, as `position.order_passages` orders passages."""
    passages = [Passage(n=i, chunk_id=l.chunk_id, book_id=l.book_id,
                        chapter_seq=l.chapter_seq, text="") for i, l in enumerate(lines)]
    return [lines[pos.n] for pos in order_passages(passages, books).ordered]


def select_lines(conn: sqlite3.Connection, lines: Sequence[CodexLine], entity_ids: Sequence[str],
                 part_vector: list[float], lance,
                 books: Mapping[str, BookInfo] | None,
                 spread: int = SPREAD, relevant: int = RELEVANT) -> list[CodexLine]:
    """At most spread + relevant lines, in story order: all of them when that
    few, else `spread` evenly spaced (always the earliest and the latest) plus
    the `relevant` nearest the part among the rest."""
    ordered = order_lines(lines, books)
    if len(ordered) <= spread + relevant:
        return ordered
    last = len(ordered) - 1
    pick = sorted({round(i * last / max(spread - 1, 1)) for i in range(spread)})
    chosen = set(pick)
    ids = _ids_of(conn, entity_ids)
    keyed = {i: ids.get((l.subject, l.text, l.chunk_id)) for i, l in enumerate(ordered)}
    rest = {keyed[i]: i for i in range(len(ordered)) if i not in chosen and keyed[i]}
    for row in lance.search_codex(part_vector, list(rest), relevant):
        at = rest.get(str(row["id"]))
        if at is not None:
            chosen.add(at)
    return [ordered[i] for i in sorted(chosen)]


def part_codex(conn: sqlite3.Connection, lance, seeds: Iterable[SeedEntity],
               part_vector: list[float], books: Mapping[str, BookInfo] | None,
               book_ids: Sequence[str] | None = None,
               limit: int = ENTITIES_PER_PART) -> list[CodexLine]:
    """The selected codex lines for one focused part: the entries of its first
    `limit` seeds that have one. [] with DEEP off, no entry, or no seed."""
    if not active():
        return []
    ids = list(dict.fromkeys(s.id for s in seeds))
    try:
        have = set(codex_store.with_entries(conn, ids))
    except sqlite3.OperationalError:  # a library not migrated to the codex yet
        return []
    entities = [i for i in ids if i in have][:limit]
    if not entities:
        return []
    lines = lines_for(conn, entities, book_ids=book_ids or None)
    if not lines:
        return []
    return select_lines(conn, lines, entities, part_vector, lance, books)
