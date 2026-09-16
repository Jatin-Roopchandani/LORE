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


def relevance(question_vector: list[float],
              chunk_vectors: dict[str, list[float]]) -> dict[str, float]:
    """chunk_id -> cosine similarity to the question, as a plain dot product.

    Both sides are unit-normalised nomic vectors (measured |v| = 1.0000 on
    the live index, see seed.py), so the dot product IS the cosine; no
    re-normalisation is done. A chunk with no stored vector is simply
    absent, and `assemble` ranks an absent chunk last.
    """
    return {
        chunk_id: sum(q * c for q, c in zip(question_vector, vector))
        for chunk_id, vector in chunk_vectors.items()
    }


def _round_robin(books: dict[str, list[_Item]]) -> list[_Item]:
    """One item from each book per turn, books in book_id order.

    Without this a cap cuts one whole book: in reading order that is always
    the later book id, and by relevance it is whichever book scores lower,
    so a cross-book question would silently lose a book.
    """
    if len(books) < 2:
        return [item for items in books.values() for item in items]
    return [item for turn in zip_longest(*(books[b] for b in sorted(books)))
            for item in turn if item is not None]


def _interleaved(items: list[_Item]) -> list[_Item]:
    """Round-robin across books, chapter order within each."""
    books: dict[str, list[_Item]] = {}
    for item in items:  # items arrive in (book, chapter) order
        books.setdefault(item[1], []).append(item)
    return _round_robin(books)


def _relevance_key(scores: dict[str, float], chunk_id: str) -> tuple[bool, float]:
    """Most relevant first, and a chunk with no score after every scored
    one, whatever the sign of their cosines."""
    return (chunk_id not in scores, -scores.get(chunk_id, 0.0))


def _by_relevance(items: list[_Item], scores: dict[str, float]) -> list[_Item]:
    """Each book's items most-relevant first (reading order breaks ties),
    then round-robin across books."""
    books: dict[str, list[_Item]] = {}
    for item in sorted(items, key=lambda i: (_relevance_key(scores, i[0]), i[1], i[2], i[0])):
        books.setdefault(item[1], []).append(item)
    return _round_robin(books)


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
             max_chars: int = MAX_EVIDENCE_CHARS,
             scores: dict[str, float] | None = None) -> Evidence:
    """Deduped, selected, capped evidence for the rows a template returned.

    Without `scores`, passages are selected in reading order (round-robin
    across books) and the cap cuts the latest chapters. With `scores`
    (chunk_id -> relevance to the question, from `relevance`), passages are
    selected most-relevant first, still round-robin across books, so the
    cap cuts the LEAST RELEVANT passages instead: Task 9's graph agent lost
    to plain vector search because a chapter-ordered cut kept each book's
    earliest chapters and dropped the ones that answered the question.
    Either way the kept passages are numbered in (book, chapter) order.

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
    selection = _interleaved(items) if scores is None else _by_relevance(items, scores)
    kept, truncated = _capped(selection, max_chars)

    if scores is not None:
        # Relationship lines follow their passage's relevance (stable, so
        # equally relevant edges keep the template's order).
        edges = tuple(sorted(edges, key=lambda r: _relevance_key(scores, r.chunk_id)))

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
