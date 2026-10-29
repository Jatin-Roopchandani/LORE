"""Book + chunk registration: one transaction, idempotent by book_id."""
from __future__ import annotations

import sqlite3

from lore.ingest.models import Chunk
from lore.query.models import BookInfo
from lore.worlds import suggest_world


def ingest_book(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    title: str,
    author: str | None,
    source: str,
    file_path: str,
    chunks: list[Chunk],
    series: str | None = None,
    series_index: float | None = None,
    world_id: int | None = None,
) -> bool:
    """Register a book. Every book gets a world: `world_id`, else the
    suggested one for its series/title (created when it does not exist)."""
    row = conn.execute(
        "SELECT status FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if row is not None:
        # Any existing books row means this book has already been through
        # ingestion (or is mid-ingestion) — not just status == 'chunked'.
        # Falling through to INSERT OR REPLACE for any other status would
        # delete-then-reinsert the parent row, which under FK enforcement
        # (PRAGMA foreign_keys = ON) can crash on chunks that already
        # reference it, or silently orphan them.
        return False
    try:
        conn.execute("BEGIN")
        if world_id is None:
            world_id, name = suggest_world(conn, series, title)
            if world_id is None:
                world_id = _new_world(conn, name, book_id)
        conn.execute(
            "INSERT OR REPLACE INTO books (id, title, author, source, file_path, status,"
            " series, series_index, world_id) VALUES (?, ?, ?, ?, ?, 'chunked', ?, ?, ?)",
            (book_id, title, author, source, file_path, series, series_index, world_id),
        )
        conn.executemany(
            "INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (c.id, c.book_id, c.chapter_seq, c.scene_seq, c.seq, c.text)
                for c in chunks
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def _new_world(conn: sqlite3.Connection, name: str, book_id: str) -> int:
    """Insert a world inside the caller's transaction; a taken name gets the
    book id appended so the book still lands somewhere of its own."""
    name = name or book_id
    if conn.execute("SELECT 1 FROM worlds WHERE name = ? COLLATE NOCASE",
                    (name,)).fetchone():
        name = f"{name} ({book_id[:8]})"
    return conn.execute("INSERT INTO worlds (name) VALUES (?)", (name,)).lastrowid


def forget_book(conn: sqlite3.Connection, book_id: str) -> bool:
    """Delete a book and everything in SQLite that hangs off it.

    Returns True if a books row was actually removed. This exists so a
    book can be re-ingested after a chunker change: chunk IDs are
    content-addressed provenance keys, so new chunking means the old
    rows are not merely stale, they are unreferenceable.

    Children go first, parent last, all in one explicit transaction.
    `PRAGMA foreign_keys = ON` is set in open_db, so jobs rows must be
    cleared too — a book that got as far as an extraction run has them,
    and leaving them makes the parent DELETE fail on the FK. Those jobs
    describe work over chunk IDs that are about to stop existing, so
    they are exactly as invalid as the chunks are.

    Explicit BEGIN/COMMIT/ROLLBACK, not `with conn:` — Phase 0 found
    that `with conn:` never issues BEGIN for DDL.
    """
    row = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        return False
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM chunks WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM jobs WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def list_books(conn: sqlite3.Connection) -> list[dict]:
    # The query path may open a library read-only that has not been migrated
    # yet: fall back to the columns it has (no world before v9, no story
    # position before v5).
    base = ("SELECT b.id, b.title, b.author, b.status, COUNT(c.id){extra}"
            " FROM books b LEFT JOIN chunks c ON c.book_id = b.id{join}"
            " GROUP BY b.id ORDER BY b.created_at")
    try:
        rows = conn.execute(base.format(
            extra=", b.series, b.series_index, b.world_id, w.name",
            join=" LEFT JOIN worlds w ON w.id = b.world_id")).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(*r, None, None) for r in conn.execute(base.format(
                extra=", b.series, b.series_index", join="")).fetchall()]
        except sqlite3.OperationalError:
            rows = [(*r, None, None, None, None) for r in conn.execute(
                base.format(extra="", join="")).fetchall()]
    return [
        {"id": r[0], "title": r[1], "author": r[2], "status": r[3], "chunk_count": r[4],
         "series": r[5], "series_index": r[6], "world_id": r[7], "world": r[8]}
        for r in rows
    ]


def set_series(conn: sqlite3.Connection, book_id: str, series: str | None,
               series_index: float | None) -> bool:
    """Set (or, with None, None, clear) a book's story position. False when
    there is no such book."""
    cur = conn.execute("UPDATE books SET series = ?, series_index = ? WHERE id = ?",
                       (series, series_index, book_id))
    conn.commit()
    return cur.rowcount > 0


def book_rows(conn: sqlite3.Connection) -> dict[str, BookInfo]:
    """Every book's title and story position, by id. Tolerates a library
    that has not been migrated yet (no series columns): the query path may
    open the file read-only as it is, and then no book has a position."""
    try:
        rows = conn.execute("SELECT id, title, series, series_index FROM books").fetchall()
    except sqlite3.OperationalError:
        rows = [(r[0], r[1], None, None)
                for r in conn.execute("SELECT id, title FROM books").fetchall()]
    return {r[0]: BookInfo(title=r[1], series=r[2], series_index=r[3]) for r in rows}


def chunks_for_book(conn: sqlite3.Connection, book_id: str) -> list[Chunk]:
    rows = conn.execute(
        "SELECT id, book_id, chapter_seq, scene_seq, seq, text FROM chunks"
        " WHERE book_id = ? ORDER BY chapter_seq, seq",
        (book_id,),
    ).fetchall()
    return [
        Chunk(
            id=r[0], book_id=r[1], chapter_seq=r[2],
            scene_seq=r[3], seq=r[4], text=r[5],
        )
        for r in rows
    ]


def chunks_by_id(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """Chunk text for the given ids. Unknown ids are simply absent."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, text FROM chunks WHERE id IN ({marks})", ids
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def find_books(conn: sqlite3.Connection, ref: str) -> list[dict]:
    """The books a user's reference names: an exact id; else a title,
    ignoring case, exactly; else a title containing the text. One entry is
    a match; several mean the reference is ambiguous; none, unknown."""
    books = list_books(conn)
    ref = ref.strip()
    for matches in (
        [b for b in books if b["id"] == ref],
        [b for b in books if b["title"].casefold() == ref.casefold()],
        [b for b in books if ref and ref.casefold() in b["title"].casefold()],
    ):
        if matches:
            return matches
    return []
