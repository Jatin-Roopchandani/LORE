"""Run one template and turn its rows into citable evidence."""
from __future__ import annotations

import sqlite3
from itertools import zip_longest
from typing import Iterable, Sequence

from lore.ingest.registry import chunks_by_id
from lore.query.models import EdgeRow, Evidence, Passage, TemplateCall
from lore.query.retrieval import drop_boilerplate
from lore.query.templates import MAX_EVIDENCE_CHARS, TEMPLATES
from lore.resolve.entityhood import is_boilerplate_chunk

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
            subject_id=str(row[9]), object_id=str(row[10]),
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


def _edge_key(edge: EdgeRow) -> tuple:
    """A total order on an edge's content.

    Appended to every relevance sort: edges of one passage all tie on
    relevance, and a stable sort would then keep whatever order the rows
    arrived in — which, before the templates were given a total order, was a
    different order on every run.
    """
    return (edge.book_id, edge.chapter_seq, edge.chunk_id, edge.predicate,
            edge.predicate_raw, edge.subject, edge.object, edge.confidence,
            edge.evidence)


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
    # Sorted on content so the leads read the same way on every run,
    # whatever order the rows arrived in.
    edges = tuple(sorted(dict.fromkeys(rows), key=_edge_key))

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
        edges = tuple(sorted(
            edges,
            key=lambda r: (_relevance_key(scores, r.chunk_id), _edge_key(r))))

    # Number in reading order so citations still read chronologically.
    rank = {item[0]: index for index, item in enumerate(items)}
    kept.sort(key=lambda item: rank[item[0]])
    passages = tuple(
        Passage(n=index + 1, chunk_id=c, book_id=b, chapter_seq=s, text=t)
        for index, (c, b, s, t) in enumerate(kept)
    )
    return Evidence(edges=edges, passages=passages, truncated=truncated)


def _pure_relevance(items: list[_Item], scores: dict[str, float]) -> list[_Item]:
    """Most relevant first across every book (reading order breaks ties)."""
    return sorted(items, key=lambda i: (_relevance_key(scores, i[0]), i[1], i[2], i[0]))


class _Turns:
    """What each list took, and passed over as already taken, per turn."""

    def __init__(self, parts: int) -> None:
        self.merged: list[_Item] = []
        self.took: list[list[tuple[int, int]]] = [[] for _ in range(parts)]  # (turn, index)
        self.skipped: list[list[tuple[int, str]]] = [[] for _ in range(parts)]  # (turn, id)


def _next_unseen(lists: list[list[_Item]]) -> _Turns:
    """One item from each list per turn, each list's next item not yet taken.

    So every sub-question gets its best passage before any gets a second.
    """
    turns = _Turns(len(lists))
    seen: set[str] = set()
    positions = [0] * len(lists)
    turn = 0
    while True:
        moved = False
        for index, items in enumerate(lists):
            while positions[index] < len(items) and items[positions[index]][0] in seen:
                turns.skipped[index].append((turn, items[positions[index]][0]))
                positions[index] += 1
            if positions[index] < len(items):
                item = items[positions[index]]
                seen.add(item[0])
                turns.took[index].append((turn, len(turns.merged)))
                turns.merged.append(item)
                moved = True
        if not moved:
            return turns
        turn += 1


def _shares(turns: _Turns, kept_count: int) -> list[set[str]]:
    """Each part's chunk ids: what it took within the budget, plus what it
    passed over as already taken on its way there.

    "On its way there" is up to the last turn in which it took a kept
    passage (its first turn if it took none): past that, every list runs
    through the whole pool, and counting those skips would hand every part
    every passage.
    """
    shares: list[set[str]] = []
    for took, skipped in zip(turns.took, turns.skipped):
        own = [(turn, index) for turn, index in took if index < kept_count]
        last = own[-1][0] if own else 0
        ids = {turns.merged[index][0] for _, index in own}
        ids.update(chunk_id for turn, chunk_id in skipped if turn <= last)
        shares.append(ids)
    return shares


def assemble_ranked(
    conn: sqlite3.Connection,
    rows: list[EdgeRow],
    *,
    score_lists: Sequence[dict[str, float]],
    extra_chunks: Iterable[tuple[str, str, int]] = (),
    max_chars: int = MAX_EVIDENCE_CHARS,
    book_fair: bool = False,
) -> Evidence:
    """Evidence from template rows plus vector passages, for one or more parts.

    `extra_chunks` are (chunk_id, book_id, chapter_seq) of passages that come
    from vector search rather than from an edge (Task 10d): the graph
    evidence never holds less than the baseline had. A chunk from both
    sources appears once.

    `score_lists` holds one chunk_id -> relevance map per sub-question. Each
    becomes its own ranked list, and the budget is filled round-robin across
    the lists, so every part gets passages. Within a list the ranking is
    purely by relevance (Task 10e): a question about each book is already
    one part per book, and book round-robin inside a one-book part handed
    half its budget to the other book. `book_fair=True` restores the
    round-robin across books within each list, as in `assemble`, for the
    no-decomposition fallback, where one part may still span books.

    Passages are numbered in reading order, and only edges whose passage
    was kept are returned. `shares` gives each part's passage numbers: the
    ones it took, plus the ones it ranked above them that another part took.
    """
    edges = tuple(dict.fromkeys(rows))
    positions: dict[str, tuple[str, int]] = {}
    for row in edges:
        positions.setdefault(row.chunk_id, (row.book_id, row.chapter_seq))
    for chunk_id, book_id, chapter_seq in extra_chunks:
        positions.setdefault(chunk_id, (book_id, chapter_seq))
    if not positions:
        return Evidence(edges=(), passages=(), truncated=False)

    ordered = sorted(positions.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    texts = chunks_by_id(conn, [c for c, _ in ordered])
    # Whatever path a chunk came from (edge, vector, keyword), a licence or
    # table-of-contents chunk is no evidence.
    items = [(c, b, s, texts[c]) for c, (b, s) in ordered
             if c in texts and not is_boilerplate_chunk(texts[c])]
    rank_list = _by_relevance if book_fair else _pure_relevance
    lists = [rank_list(items, scores) for scores in score_lists] or [_interleaved(items)]
    turns = _next_unseen(lists)
    kept, truncated = _capped(turns.merged, max_chars)

    kept_ids = {item[0] for item in kept}

    def best(chunk_id: str) -> tuple[bool, float]:
        return min(_relevance_key(scores, chunk_id) for scores in score_lists) \
            if score_lists else (False, 0.0)

    edges = tuple(sorted((e for e in edges if e.chunk_id in kept_ids),
                         key=lambda e: (best(e.chunk_id), _edge_key(e))))
    rank = {item[0]: index for index, item in enumerate(items)}
    kept.sort(key=lambda item: rank[item[0]])
    passages = tuple(
        Passage(n=index + 1, chunk_id=c, book_id=b, chapter_seq=s, text=t)
        for index, (c, b, s, t) in enumerate(kept)
    )
    number = {p.chunk_id: p.n for p in passages}
    shares = tuple(tuple(sorted(number[c] for c in share if c in number))
                   for share in _shares(turns, len(kept)))
    return Evidence(edges=edges, passages=passages, truncated=truncated, shares=shares)


def passages_from_chunks(chunk_rows: list[dict]) -> tuple[Passage, ...]:
    """Passages straight from LanceDB rows, for the no-graph paths."""
    chunk_rows = drop_boilerplate(chunk_rows)
    return tuple(
        Passage(n=index + 1, chunk_id=str(row["chunk_id"]),
                book_id=str(row.get("book_id", "")),
                chapter_seq=int(row.get("chapter_seq", 0)),
                text=str(row.get("text", "")))
        for index, row in enumerate(chunk_rows)
    )
