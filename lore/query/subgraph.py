"""Run one template and turn its rows into citable evidence."""
from __future__ import annotations

import sqlite3
from itertools import zip_longest

from lore.ingest.registry import chunks_by_id
from lore.query.models import EdgeRow, Evidence, Passage, TemplateCall
from lore.query.templates import MAX_EVIDENCE_CHARS, TEMPLATES

_Item = tuple[str, str, int, str]  # chunk_id, book_id, chapter_seq, text


def run_template(kuzu, call: TemplateCall) -> list[EdgeRow]:
    """Execute a template. Slot values bind as parameters, never as text."""
    template = TEMPLATES[call.template]
    rows = kuzu.execute(template.cypher, dict(call.slots))
    return [
        EdgeRow(
            subject=str(row[0]), predicate=str(row[1]), predicate_raw=str(row[2]),
            object=str(row[3]), evidence=str(row[4]), confidence=float(row[5]),
            book_id=str(row[6]), chapter_seq=int(row[7]), chunk_id=str(row[8]),
        )
        for row in rows
    ]


def _interleaved(items: list[_Item]) -> list[_Item]:
    """Round-robin across books, in book_id order, chapter order within each.

    Without this the cap cuts from the end of the reading-order list, and
    because book ids sort, that end is always one whole book: a cross-book
    question would silently lose its second book.
    """
    books: dict[str, list[_Item]] = {}
    for item in items:  # items arrive in (book, chapter) order
        books.setdefault(item[1], []).append(item)
    if len(books) < 2:
        return items
    return [item for turn in zip_longest(*books.values()) for item in turn
            if item is not None]


def _capped(items: list[_Item], max_chars: int) -> tuple[list[_Item], bool]:
    kept: list[_Item] = []
    used = 0
    for item in items:
        # Always keep the first passage: an answer with no evidence at all
        # is worse than one over budget by a little.
        if kept and used + len(item[3]) > max_chars:
            return kept, True
        kept.append(item)
        used += len(item[3])
    return kept, False


def assemble(conn: sqlite3.Connection, rows: list[EdgeRow], *,
             max_chars: int = MAX_EVIDENCE_CHARS) -> Evidence:
    """Deduped, ordered, capped evidence for the rows a template returned.

    Chunk text lives in SQLite, not in Kuzu's Chunk node -- the node
    carries only ids and positions.
    """
    if not rows:
        return Evidence(edges=(), passages=(), truncated=False)

    # Templates re-emit the same edge once per intermediate node, and the
    # graph holds parallel edges with identical content. Exact equality
    # drops only true repeats; two different evidence strings both stay.
    edges = tuple(dict.fromkeys(rows))

    ordered: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for row in sorted(edges, key=lambda r: (r.book_id, r.chapter_seq, r.chunk_id)):
        if row.chunk_id not in seen:
            seen.add(row.chunk_id)
            ordered.append((row.chunk_id, row.book_id, row.chapter_seq))

    texts = chunks_by_id(conn, [c for c, _, _ in ordered])
    items = [(c, b, s, texts[c]) for c, b, s in ordered if c in texts]
    kept, truncated = _capped(_interleaved(items), max_chars)

    # Number in reading order so citations still read chronologically.
    rank = {item[0]: index for index, item in enumerate(items)}
    kept.sort(key=lambda item: rank[item[0]])
    passages = tuple(
        Passage(n=index + 1, chunk_id=c, book_id=b, chapter_seq=s, text=t)
        for index, (c, b, s, t) in enumerate(kept)
    )
    return Evidence(edges=edges, passages=passages, truncated=truncated)


def passages_from_chunks(chunk_rows: list[dict]) -> tuple[Passage, ...]:
    """Passages straight from LanceDB rows, for the no-graph paths."""
    return tuple(
        Passage(n=index + 1, chunk_id=str(row["chunk_id"]),
                book_id=str(row.get("book_id", "")),
                chapter_seq=int(row.get("chapter_seq", 0)),
                text=str(row.get("text", "")))
        for index, row in enumerate(chunk_rows)
    )
