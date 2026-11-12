"""Deleting a book from a reader's library (DELETE /api/books/{id}).

The removal itself is `lore.sample.delete_book`, the one `lore sample make`
uses: out of the vector indexes and the graph (shared entities keep their
other books, and a node goes only when no book is left), its processing
state, its SQLite rows and its files (extractions, beats, pending
disambiguation questions, narrator and speaker answers, the stored upload).
Around it, what a library in use needs and a template does not:

* Refusals. The book must not be processing: a fresh `running` row, or (the
  caller's `check`, multi mode) a queued or claimed job in the central
  queue. A stopped book (failed, waiting for the allowance or a key, idle)
  can be deleted: nothing is writing it. Then the library's lock is taken
  EXCLUSIVE (lore.library_lock): every run and upload holds it shared, so a
  delete waits for nothing and overlaps nothing; it is refused instead.
* Reading progress: the book leaves its world's progress list.
* The world: when the book was its last, the world goes too, with its
  position and anything keyed by it, as the template drops empty worlds.
  (A world the reader emptied by moving books out stays, as before: that
  one they made or kept on purpose.)
* The codex: the removal drops the entry of every entity the book touched,
  shared ones included. The entries of the world's remaining books come
  back from the answers already saved (the template's cached codex; nothing
  is asked of a model), and those with no saved answer are reported: the
  next processing run in that world rebuilds them. Saved answers of the
  book's entities other than the current ones (they quote the deleted book)
  and the entity intros written for those entities go.

Left as they are: the adjudication and disambiguation answers (keyed by a
hash of the question, holding only yes/no verdicts, shared across books),
the gemini/quota ledgers and stage timings (usage, not the book), and the
reader's chats in the central database (past answers are history).

The stores are not rewritten: Kuzu keeps deleted strings in freed pages,
LanceDB keeps older versions of a table until it is compacted, and SQLite
keeps freed pages. None of that is reachable through the app, it is all in
the reader's own library directory, and it goes when the account is
erased; the template command rewrites them only because a template is
copied into every new account.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from lore import library_lock
from lore.config import LoreConfig

log = logging.getLogger(__name__)


class DeleteRefused(Exception):
    """The book cannot be deleted now. `code`: not_found, processing or busy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class DeleteReport:
    book_id: str
    title: str
    world: str | None
    world_removed: bool = False
    codex_missing: list[str] = field(default_factory=list)


PROCESSING = "this book is still being processed; let it finish first"
BUSY = ("a book is being added or processed in your library;"
        " try again when it is done")


def delete_book(config: LoreConfig, book_id: str, *, now: datetime | None = None,
                check: Callable[[str], DeleteRefused | None] | None = None,
                codex_refresh: Callable | None = None) -> DeleteReport:
    """Delete `book_id` from the library `config` names. Raises DeleteRefused.

    `check(book_id)`: the caller's own refusal (multi mode: a live job in the
    central queue), asked once the book is known to exist and before the
    library is locked. `codex_refresh(config, conn, book_id)`: the cached
    codex rebuild (default: lore.sample's, which asks no model)."""
    from lore.db.sqlite import open_db
    from lore.orchestrator import jobs

    moment = now or datetime.now(timezone.utc)
    conn = open_db(config.sqlite_path)
    try:
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise DeleteRefused("not_found", f"no book with id {book_id!r}")
        job = jobs.get_job(conn, book_id)
        if job is not None and jobs.is_fresh(job, moment):
            raise DeleteRefused("processing", PROCESSING)
    finally:
        conn.close()
    if check is not None and (refused := check(book_id)) is not None:
        raise refused
    try:
        with library_lock.hold(config.data_dir, exclusive=True):
            return _delete_locked(config, book_id, moment, codex_refresh)
    except library_lock.LibraryBusy:
        raise DeleteRefused("busy", BUSY) from None


def _delete_locked(config: LoreConfig, book_id: str, moment: datetime,
                   codex_refresh: Callable | None) -> DeleteReport:
    from lore import sample, worlds
    from lore.db.kuzu_store import KuzuStore
    from lore.db.lance_store import LanceStore
    from lore.db.sqlite import open_db
    from lore.orchestrator import jobs

    conn = open_db(config.sqlite_path)
    try:
        # Read again under the lock: a run that ended a moment ago, or a
        # delete of the same book that just finished, is seen now.
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise DeleteRefused("not_found", f"no book with id {book_id!r}")
        job = jobs.get_job(conn, book_id)
        if job is not None and jobs.is_fresh(job, moment):
            raise DeleteRefused("processing", PROCESSING)
        world = worlds.world_of(conn, book_id)
        report = DeleteReport(book_id, row[0], world.name if world else None)

        kuzu = KuzuStore(config.kuzu_dir / "graph")
        try:
            touched = sorted(r[0] for r in kuzu.execute(
                "MATCH (e:Entity) WHERE list_contains(e.books, $book) RETURN e.id",
                {"book": book_id}))
            sample.delete_book(config, conn, kuzu, LanceStore(config.lancedb_dir), book_id)
        finally:
            kuzu.close()

        if touched:
            with conn:
                conn.execute(f"DELETE FROM entity_intros WHERE entity_id IN"  # noqa: S608
                             f" ({', '.join('?' for _ in touched)})", touched)
        if world is not None:
            if worlds.books_in(conn, world.id):
                _leave_progress(conn, world.id, book_id)
                report.codex_missing = _refresh_codex(
                    config, conn, worlds.books_in(conn, world.id)[0],
                    codex_refresh or sample.cached_codex())
            else:
                _drop_world(conn, world.id)
                report.world_removed = True
        _prune_codex_cache(config, conn, touched)
        return report
    finally:
        conn.close()


def _leave_progress(conn: sqlite3.Connection, world_id: int, book_id: str) -> None:
    """The world's stored progress without the book."""
    from lore import reading_position as rp

    position = rp.get_position(conn, world_id)
    if position is None:
        return
    if position.progress is not None:
        old = position.progress
        new = rp.Progress.of({b: c for b, c in old.reading if b != book_id},
                             [b for b in old.unread if b != book_id])
        if new != old:
            rp.set_position(conn, rp.progress_position(world_id, new))
        return
    # The older shape (a library not yet migrated to progress lists).
    if position.book_id != book_id and book_id not in position.read_books:
        return
    same = position.book_id == book_id
    rp.set_position(conn, rp.ReadingPosition(
        world_id, None if same else position.book_id, 0 if same else position.chapter,
        tuple(b for b in position.read_books if b != book_id), position.enabled))


def _drop_world(conn: sqlite3.Connection, world_id: int) -> None:
    """An emptied world, and what is keyed by it."""
    from lore import worlds
    from lore.reading_position import delete_position

    delete_position(conn, world_id)
    worlds.delete_world(conn, world_id)
    with conn:
        for table in ("codex_summaries", "codex_entries", "entity_intros"):
            conn.execute(f"DELETE FROM {table} WHERE world_id = ?", (world_id,))  # noqa: S608


def _refresh_codex(config: LoreConfig, conn: sqlite3.Connection, other: str,
                   refresh: Callable) -> list[str]:
    """Rebuild, from saved answers, the codex entries of `other`'s world that
    the removal dropped. The stage reports through `other`'s job row, so the
    row is put back exactly as it was. Never raises: the book is gone either
    way; a failure is logged and reported as entries missing."""
    saved = conn.execute("SELECT * FROM book_jobs WHERE book_id = ?", (other,)).fetchone()
    try:
        refresh(config, conn, other)
        missing = list(getattr(refresh, "missing", []) or [])
    except Exception:  # noqa: BLE001
        log.exception("rebuilding the codex after a delete failed")
        missing = ["(the codex could not be rebuilt)"]
    finally:
        _put_back_job(conn, other, saved)
    return missing


def _put_back_job(conn: sqlite3.Connection, book_id: str, saved) -> None:
    cols = [d[1] for d in conn.execute("PRAGMA table_info(book_jobs)")]
    with conn:
        conn.execute("DELETE FROM book_jobs WHERE book_id = ?", (book_id,))
        if saved is not None:
            conn.execute(f"INSERT INTO book_jobs ({', '.join(cols)}) VALUES"  # noqa: S608
                         f" ({', '.join('?' for _ in cols)})", list(saved))


def _prune_codex_cache(config: LoreConfig, conn: sqlite3.Connection,
                       touched: list[str]) -> None:
    """Saved codex answers of the book's entities, except each one's current
    entry: the others were built from evidence that included the book."""
    if not touched or not config.codex_dir.is_dir():
        return
    current = {f"{entity}-{evidence}" for entity, evidence in conn.execute(
        "SELECT entity_id, evidence_hash FROM codex_summaries")}
    prefixes = tuple(f"{entity}-" for entity in touched)
    for path in config.codex_dir.iterdir():
        stem = path.name.split(".", 1)[0]
        if path.is_file() and path.name.startswith(prefixes) and stem not in current:
            path.unlink(missing_ok=True)
