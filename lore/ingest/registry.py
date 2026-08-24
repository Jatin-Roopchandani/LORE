"""Book + chunk registration: one transaction, idempotent by book_id."""
from __future__ import annotations

import sqlite3

from lore.ingest.models import Chunk


def ingest_book(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    title: str,
    author: str | None,
    source: str,
    file_path: str,
    chunks: list[Chunk],
) -> bool:
    row = conn.execute(
        "SELECT status FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if row is not None and row[0] == "chunked":
        return False
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT OR REPLACE INTO books (id, title, author, source, file_path, status)"
            " VALUES (?, ?, ?, ?, ?, 'chunked')",
            (book_id, title, author, source, file_path),
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


def list_books(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.id, b.title, b.author, b.status, COUNT(c.id)"
        " FROM books b LEFT JOIN chunks c ON c.book_id = b.id"
        " GROUP BY b.id ORDER BY b.created_at"
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "author": r[2], "status": r[3], "chunk_count": r[4]}
        for r in rows
    ]
