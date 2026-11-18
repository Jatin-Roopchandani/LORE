"""Books in a question's scope that cannot be searched yet, and what to say.

A book gets its passages' vectors and its graph only when processing reaches
the load stage, so until then a question can find nothing in it. `unready`
lists the scope's books in that state, read from the library (book rows, their
jobs, LanceDB): no model call. `explain` is the plain text an answer that came
back empty gives instead of the refusal: which book, how far it got, when it
continues, and to ask again when it is ready.

A book counts as unready only when it is not `graphed` AND has no vectors, so
a library whose books are all processed takes no further step than one cheap
query (and a book whose status lags its data is never called unsearchable).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from lore.orchestrator import jobs as jobs_module
from lore.query.models import ProcessingBook
from lore.query.scope import Scope

# What each stage is doing, for "(… of … parts read)" and its kin.
_STAGE = {"chunk": "splitting it into parts", "load": "building its graph",
          "remove": "taking it out of the graph", "reassign": "changing its world",
          "disambiguate": "merging duplicate entities", "beats": "finding story beats",
          "codex": "writing entity profiles", "ready": "finishing up"}

READY_EMPTY = "The books that are ready don't contain enough to answer that."


def _has_vectors(lance, book_id: str) -> bool:
    has = getattr(lance, "book_has_chunks", None)
    if has is None:  # not a LanceStore (a test double): never call a book unsearchable
        return True
    try:
        if "chunks" not in lance.table_names():
            return False
        return bool(has(book_id))
    except Exception:  # noqa: BLE001 -- a lookup for a hint never fails an answer
        return True


def _state(job: jobs_module.BookJob | None, now: datetime) -> str:
    if job is None:
        return "not_started"
    if job.state == jobs_module.RUNNING and not jobs_module.is_fresh(job, now):
        return "idle"  # interrupted: nothing is moving it on
    return job.state


def unready(conn: sqlite3.Connection, lance, scopes: list[Scope], *,
            now: datetime | None = None) -> tuple[tuple[ProcessingBook, ...], bool]:
    """(the scopes' books that cannot be searched yet, in library order;
    whether the scopes hold any book that can). `scopes` should be unlimited by
    a reading position, so a book is judged by its data, not by the position."""
    try:
        rows = conn.execute("SELECT id, title, status FROM books"
                            " ORDER BY created_at, id").fetchall()
    except sqlite3.OperationalError:
        return (), True
    candidates = [r for r in rows if r[2] != "graphed"]
    if not candidates:
        return (), True
    in_scope = [r for r in rows if any(s.allows(r[0]) for s in scopes)]
    moment = now or datetime.now(timezone.utc)
    out: list[ProcessingBook] = []
    for book_id, title, status in in_scope:
        if status == "graphed" or _has_vectors(lance, book_id):
            continue
        try:
            job = jobs_module.get_job(conn, book_id)
        except sqlite3.OperationalError:  # a library from before book_jobs
            job = None
        out.append(ProcessingBook(
            book_id=book_id, title=title or book_id,
            stage=job.stage if job else None, state=_state(job, moment),
            done=job.done_items if job else None, total=job.total_items if job else None,
            resumable_at=(job.resumable_at.isoformat()
                          if job and job.resumable_at and job.state == jobs_module.WAITING_QUOTA
                          else None)))
    return tuple(out), len(in_scope) > len(out)


def _progress(book: ProcessingBook) -> str | None:
    if book.stage is None:
        return None
    if book.stage == "extract":
        return (f"{book.done:,} of {book.total:,} parts read" if book.total
                else "reading its parts")
    what = _STAGE.get(book.stage, book.stage)
    return f"{what}, {book.done:,} of {book.total:,}" if (book.total or 0) > 1 else what


def _when(book: ProcessingBook) -> str:
    if book.state == "running":
        return "It is processing now."
    if book.state == "waiting_quota" and book.resumable_at:
        at = datetime.fromisoformat(book.resumable_at).astimezone(timezone.utc)
        return f"It continues automatically after {at:%H:%M} UTC."
    if book.state == "waiting_key":
        return "It is paused until your AI provider's key is fixed in Settings."
    if book.state == "failed":
        return "Its processing stopped with an error: press Retry in the Library."
    return "It is paused: press Continue in the Library."


def _sentence(book: ProcessingBook) -> str:
    if book.state == "not_started":
        return f"{book.title} hasn't been processed yet, so it can't be searched."
    progress = _progress(book)
    got = f" ({progress})" if progress else ""
    return f"{book.title} is still being processed{got}, so it can't be searched yet. {_when(book)}"


def explain(books: tuple[ProcessingBook, ...], *, others_ready: bool) -> str:
    """The text an empty answer gives when `books` could not be searched."""
    lead = [READY_EMPTY] if others_ready else []
    close = "Ask again when it's ready." if len(books) == 1 else "Ask again when they're ready."
    return " ".join([*lead, *(_sentence(b) for b in books), close])
