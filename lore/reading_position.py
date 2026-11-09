"""Where a reader is, per world: the stored reading progress.

A world's row says, for each of its books, whether the reader has finished
it, is reading it (and up to which chapter) or has not started it. Finished
is the default: only the books in progress (`Progress.reading`, book id ->
the last chapter_seq finished) and the books not started (`Progress.unread`)
are stored, so a book added to the world later is finished until the reader
says otherwise and nothing is hidden that they did not ask to hide. What the
progress makes visible is decided in lore/query/visible.py (which also never
shows a book past an earlier book of its series that is not finished).

Rows written before the progress list (migration 18 converts them) had an
older shape, still accepted here: `(book_id, chapter)`, the one book being
read; `read_books`, the books of unknown order against it marked as read;
`enabled`, the switch. A position whose `progress` is None is in that shape.
For a progress row `enabled` is derived: whether anything is limited, and
`book_id`/`chapter` mirror its first book in progress.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Progress:
    """The books not finished: in progress (with the chapter_seq reached) or
    not started. Every other book of the world is finished."""

    reading: tuple[tuple[str, int], ...] = ()
    unread: tuple[str, ...] = ()

    @classmethod
    def of(cls, reading: Mapping[str, int] | Iterable[tuple[str, int]] = (),
           unread: Iterable[str] = ()) -> "Progress":
        pairs = dict(reading.items() if isinstance(reading, Mapping) else reading)
        pairs = {str(b): max(0, int(c)) for b, c in pairs.items() if b}
        return cls(tuple(sorted(pairs.items())),
                   tuple(sorted({str(b) for b in unread if b and b not in pairs})))

    @property
    def chapters(self) -> dict[str, int]:
        return dict(self.reading)

    def limits(self) -> bool:
        return bool(self.reading or self.unread)

    def to_json(self) -> str:
        return json.dumps({"reading": dict(self.reading), "unread": list(self.unread)})

    @classmethod
    def from_json(cls, text: str | None) -> "Progress | None":
        if text is None:
            return None
        try:
            data = json.loads(text) or {}
            return cls.of(data.get("reading") or {}, data.get("unread") or ())
        except (TypeError, ValueError, AttributeError):
            return cls()


@dataclass(frozen=True)
class ReadingPosition:
    world_id: int
    book_id: str | None
    chapter: int
    read_books: tuple[str, ...] = ()
    enabled: bool = False
    updated_at: str = ""
    # The progress list; None for a position in the older shape (see above).
    progress: Progress | None = None


def progress_position(world_id: int, progress: Progress) -> ReadingPosition:
    """A position holding `progress` (see the module docstring)."""
    first = progress.reading[0] if progress.reading else (None, 0)
    return ReadingPosition(world_id, first[0], first[1], (), progress.limits(), "", progress)


_COLUMNS = "world_id, book_id, chapter, read_books, enabled, updated_at, progress"


def _row(r) -> ReadingPosition:
    try:
        read = tuple(str(b) for b in json.loads(r[3] or "[]"))
    except (TypeError, ValueError):
        read = ()
    return ReadingPosition(world_id=r[0], book_id=r[1], chapter=int(r[2] or 0),
                           read_books=read, enabled=bool(r[4]), updated_at=r[5] or "",
                           progress=Progress.from_json(r[6]))


def _select(conn: sqlite3.Connection, where: str, args=()) -> list:
    try:
        return conn.execute(f"SELECT {_COLUMNS} FROM reading_positions {where}", args).fetchall()
    except sqlite3.OperationalError:
        try:  # a library from before the progress column
            rows = conn.execute(f"SELECT {_COLUMNS.replace(', progress', '')}"
                                f" FROM reading_positions {where}", args).fetchall()
        except sqlite3.OperationalError:  # or from before reading positions
            return []
        return [(*r, None) for r in rows]


def get_position(conn: sqlite3.Connection, world_id: int) -> ReadingPosition | None:
    """The world's stored position, or None (also for a library not yet
    migrated to reading positions)."""
    rows = _select(conn, "WHERE world_id = ?", (world_id,))
    return _row(rows[0]) if rows else None


def enabled_positions(conn: sqlite3.Connection) -> dict[int, ReadingPosition]:
    """Every world's position that limits anything, by world id; {} when none
    does (the common case, one cheap query)."""
    out = {}
    for r in _select(conn, "WHERE enabled = 1"):
        position = _row(r)
        if position.progress is not None or position.book_id is not None:
            out[position.world_id] = position
    return out


def set_position(conn: sqlite3.Connection, position: ReadingPosition) -> ReadingPosition:
    """Store the world's position, replacing any earlier one."""
    read = sorted({b for b in position.read_books if b})
    progress = position.progress
    enabled = progress.limits() if progress is not None else position.enabled
    with conn:
        conn.execute(
            "INSERT INTO reading_positions (world_id, book_id, chapter, read_books,"
            " enabled, progress, updated_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(world_id) DO UPDATE SET book_id = excluded.book_id,"
            " chapter = excluded.chapter, read_books = excluded.read_books,"
            " enabled = excluded.enabled, progress = excluded.progress,"
            " updated_at = excluded.updated_at",
            (position.world_id, position.book_id, max(0, int(position.chapter)),
             json.dumps(read), 1 if enabled else 0,
             progress.to_json() if progress is not None else None))
    stored = get_position(conn, position.world_id)
    assert stored is not None
    return stored


def delete_position(conn: sqlite3.Connection, world_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM reading_positions WHERE world_id = ?", (world_id,))


def migrate_to_progress(conn: sqlite3.Connection) -> None:
    """Migration 18: every stored position as a progress list that shows
    exactly what it showed (lore.query.visible.legacy_progress). A position
    that was off, or whose book is gone, showed everything: every book is
    finished."""
    from lore.ingest.registry import book_rows
    from lore.query.visible import legacy_progress

    rows = conn.execute("SELECT world_id, book_id, chapter, read_books, enabled, updated_at,"
                        " NULL FROM reading_positions").fetchall()
    if not rows:
        return
    infos = book_rows(conn)
    for r in rows:
        old = _row(r)
        world = [b[0] for b in conn.execute("SELECT id FROM books WHERE world_id = ?",
                                            (old.world_id,))]
        progress = legacy_progress(old, world, infos)
        first = progress.reading[0] if progress.reading else (None, 0)
        conn.execute("UPDATE reading_positions SET progress = ?, enabled = ?, book_id = ?,"
                     " chapter = ? WHERE world_id = ?",
                     (progress.to_json(), 1 if progress.limits() else 0, first[0], first[1],
                      old.world_id))
