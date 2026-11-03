"""Retrieval guards shared by every query path: no boilerplate passages, no
book-title seeds, and the book a part names.

Vector search, keyword search and graph evidence all return whatever is
nearest, and the nearest passages to a short question are sometimes the
Gutenberg licence or the table of contents. The graph loader already skips
those chunks; the indexes were built before it did, so they are filtered
here, at query time, with the loader's own detector (extended to tables of
contents). That needs no re-index.
"""
from __future__ import annotations

import re
from typing import Collection, Iterable, Mapping

from lore.resolve.entityhood import is_boilerplate_chunk
from lore.resolve.normalize import normalize_name

# How far past `limit` a filtered search reads: the boilerplate in a library
# is a few dozen chunks, so one widening is almost always enough.
_OVERFETCH = 24
_MAX_ROUNDS = 4


def drop_boilerplate(rows: Iterable[dict]) -> list[dict]:
    """LanceDB rows without Gutenberg licence or table-of-contents chunks."""
    return [r for r in rows if not is_boilerplate_chunk(str(r.get("text", "")))]


def search_chunks(lance, vector: list[float], limit: int, *,
                  book_id: str | None = None,
                  book_ids: Collection[str] | None = None) -> list[dict]:
    """The `limit` nearest story chunks (of one book when `book_id` is given,
    of these books when `book_ids` is; a world's books are the usual case).

    Reads past `limit` and filters, widening until `limit` survive or the
    store has no more, so a licence chunk never costs a real passage its slot.
    """
    if limit <= 0:
        return []
    if book_ids is not None and not book_ids:
        return []
    fetch = limit + _OVERFETCH
    kept: list[dict] = []
    for _ in range(_MAX_ROUNDS):
        if book_id is not None:
            if hasattr(lance, "search_chunks_in_book"):
                rows = lance.search_chunks_in_book(vector, book_id, limit=fetch)
            else:  # a store without a prefilter: filter the global ranking
                rows = [r for r in lance.search_chunks(vector, limit=fetch)
                        if r.get("book_id") == book_id]
        elif book_ids is not None:
            if hasattr(lance, "search_chunks_in_books"):
                rows = lance.search_chunks_in_books(vector, sorted(book_ids), limit=fetch)
            else:
                rows = [r for r in lance.search_chunks(vector, limit=fetch)
                        if r.get("book_id") in book_ids]
        else:
            rows = lance.search_chunks(vector, limit=fetch)
        kept = drop_boilerplate(rows)
        if len(kept) >= limit or len(rows) < fetch:
            break
        fetch *= 2
    return kept[:limit]


def title_names(titles: Iterable[str]) -> frozenset[str]:
    """Normalised library book titles: a graph node with one of these names
    was made from the book's own title, which is no character or object."""
    return frozenset(n for n in (normalize_name(t) for t in titles) if n)


_WORD = re.compile(r"\w+")


def books_named(text: str, titles: Mapping[str, str]) -> list[str]:
    """Ids of the books whose title appears in `text` as a whole phrase
    (case-insensitive, a leading article optional), in library order."""
    words = _WORD.findall(text.casefold())
    found = []
    for book_id, title in titles.items():
        tokens = _WORD.findall(normalize_name(title))
        if tokens and any(words[i:i + len(tokens)] == tokens
                          for i in range(len(words) - len(tokens) + 1)):
            found.append(book_id)
    return found
