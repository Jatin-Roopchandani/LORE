"""Where a reader is, per world: the stored reading position.

A position is `(world_id, book_id, chapter)`: the reader is in `book_id` and
has finished chapters 1..`chapter` of it. `read_books` are books whose order
against `book_id` is unknown (lore/query/position.py) that the reader says
they have read. `enabled` is the switch: a stored position that is off limits
nothing. This module is rows only; what a position makes visible is decided in
lore/query/visible.py.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ReadingPosition:
    world_id: int
    book_id: str | None
    chapter: int
    read_books: tuple[str, ...] = ()
    enabled: bool = False
    updated_at: str = ""


_COLUMNS = "world_id, book_id, chapter, read_books, enabled, updated_at"


def _row(r) -> ReadingPosition:
    try:
        read = tuple(str(b) for b in json.loads(r[3] or "[]"))
    except (TypeError, ValueError):
        read = ()
    return ReadingPosition(world_id=r[0], book_id=r[1], chapter=int(r[2] or 0),
                           read_books=read, enabled=bool(r[4]), updated_at=r[5] or "")


def get_position(conn: sqlite3.Connection, world_id: int) -> ReadingPosition | None:
    """The world's stored position, or None (also for a library not yet
    migrated to reading positions)."""
    try:
        r = conn.execute(f"SELECT {_COLUMNS} FROM reading_positions WHERE world_id = ?",
                         (world_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return _row(r) if r else None


def enabled_positions(conn: sqlite3.Connection) -> dict[int, ReadingPosition]:
    """Every world's position that is switched on, by world id; {} when none
    is (the common case, one cheap query)."""
    try:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM reading_positions"
                            " WHERE enabled = 1 AND book_id IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: _row(r) for r in rows}


def set_position(conn: sqlite3.Connection, position: ReadingPosition) -> ReadingPosition:
    """Store the world's position, replacing any earlier one."""
    read = sorted({b for b in position.read_books if b})
    with conn:
        conn.execute(
            "INSERT INTO reading_positions (world_id, book_id, chapter, read_books,"
            " enabled, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(world_id) DO UPDATE SET book_id = excluded.book_id,"
            " chapter = excluded.chapter, read_books = excluded.read_books,"
            " enabled = excluded.enabled, updated_at = excluded.updated_at",
            (position.world_id, position.book_id, max(0, int(position.chapter)),
             json.dumps(read), 1 if position.enabled else 0))
    stored = get_position(conn, position.world_id)
    assert stored is not None
    return stored


def delete_position(conn: sqlite3.Connection, world_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM reading_positions WHERE world_id = ?", (world_id,))
