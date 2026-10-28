"""The book job: one row per book saying where its processing stands.

Sibling to gemini_budget.py and the `jobs` table's per-unit rows: those say
what one extraction unit is doing, this says where the whole BOOK is, so a
run stopped by the daily Gemini allowance can be resumed (CLI or web) from
the saved stage and item count. Times are UTC, stored as ISO-8601 strings.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from lore.orchestrator.gemini_budget import GOOGLE_DAY_ZONE

UTC = timezone.utc
STALE_AFTER = timedelta(seconds=120)

RUNNING = "running"
WAITING_QUOTA = "waiting_quota"
FAILED = "failed"
DONE = "done"
IDLE = "idle"


@dataclass(frozen=True)
class BookJob:
    book_id: str
    stage: str
    state: str  # running, waiting_quota, failed, done or idle
    done_items: int
    total_items: int
    stopped_reason: str | None
    resumable_at: datetime | None  # UTC; set only when waiting_quota
    updated_at: datetime  # UTC


_COLS = ("book_id, stage, state, done_items, total_items,"
         " stopped_reason, resumable_at, updated_at")


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("a timezone-aware datetime is required, not a naive one")
    return now.astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _from_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def _row(r: tuple) -> BookJob:
    return BookJob(r[0], r[1], r[2], int(r[3]), int(r[4]), r[5],
                   _from_iso(r[6]), _from_iso(r[7]))


def get_job(conn: sqlite3.Connection, book_id: str) -> BookJob | None:
    r = conn.execute(f"SELECT {_COLS} FROM book_jobs WHERE book_id = ?",
                     (book_id,)).fetchone()
    return _row(r) if r else None


def list_jobs(conn: sqlite3.Connection) -> list[BookJob]:
    return [_row(r) for r in conn.execute(
        f"SELECT {_COLS} FROM book_jobs ORDER BY book_id")]


def next_reset_utc(now: datetime | None = None) -> datetime:
    """The next midnight Pacific (Google's free-tier reset), in UTC.

    Uses gemini_budget's zone. Built from the Pacific calendar date, not
    "now + 24h", so the 23- and 25-hour days around a DST change are right.
    """
    moment = _aware(now).astimezone(GOOGLE_DAY_ZONE)
    midnight = datetime.combine(moment.date() + timedelta(days=1), time(0),
                                tzinfo=GOOGLE_DAY_ZONE)
    return midnight.astimezone(UTC)


def _write(conn: sqlite3.Connection, book_id: str, sql: str, params: tuple) -> None:
    with conn:
        conn.execute(sql, params)


def start_job(conn: sqlite3.Connection, book_id: str, stage: str, total: int,
              done: int = 0, *, now: datetime | None = None) -> BookJob:
    """Enter `stage` as running (creating the row if needed); clears any stop."""
    ts = _iso(_aware(now))
    with conn:
        conn.execute(
            "INSERT INTO book_jobs (book_id, stage, state, done_items,"
            " total_items, stopped_reason, resumable_at, updated_at)"
            " VALUES (?, ?, 'running', ?, ?, NULL, NULL, ?)"
            " ON CONFLICT(book_id) DO UPDATE SET stage = excluded.stage,"
            " state = 'running', done_items = excluded.done_items,"
            " total_items = excluded.total_items, stopped_reason = NULL,"
            " resumable_at = NULL, updated_at = excluded.updated_at",
            (book_id, stage, done, total, ts))
    return get_job(conn, book_id)


def advance(conn: sqlite3.Connection, book_id: str, done: int, *,
            now: datetime | None = None) -> None:
    """Record progress within the stage (an absolute count); also a heartbeat."""
    _write(conn, book_id,
           "UPDATE book_jobs SET done_items = ?, updated_at = ? WHERE book_id = ?",
           (done, _iso(_aware(now)), book_id))


def heartbeat(conn: sqlite3.Connection, book_id: str, *,
              now: datetime | None = None) -> None:
    _write(conn, book_id,
           "UPDATE book_jobs SET updated_at = ? WHERE book_id = ?",
           (_iso(_aware(now)), book_id))


def finish_stage(conn: sqlite3.Connection, book_id: str, next_stage: str, *,
                 now: datetime | None = None) -> None:
    """The stage is complete: park the job, idle, at the start of `next_stage`."""
    _write(conn, book_id,
           "UPDATE book_jobs SET stage = ?, state = 'idle', done_items = 0,"
           " total_items = 0, stopped_reason = NULL, resumable_at = NULL,"
           " updated_at = ? WHERE book_id = ?",
           (next_stage, _iso(_aware(now)), book_id))


def stop_quota(conn: sqlite3.Connection, book_id: str, reason: str, *,
               now: datetime | None = None) -> None:
    moment = _aware(now)
    _write(conn, book_id,
           "UPDATE book_jobs SET state = 'waiting_quota', stopped_reason = ?,"
           " resumable_at = ?, updated_at = ? WHERE book_id = ?",
           (reason, _iso(next_reset_utc(moment)), _iso(moment), book_id))


def fail(conn: sqlite3.Connection, book_id: str, reason: str, *,
         now: datetime | None = None) -> None:
    _write(conn, book_id,
           "UPDATE book_jobs SET state = 'failed', stopped_reason = ?,"
           " resumable_at = NULL, updated_at = ? WHERE book_id = ?",
           (reason, _iso(_aware(now)), book_id))


def done(conn: sqlite3.Connection, book_id: str, *,
         now: datetime | None = None) -> None:
    _write(conn, book_id,
           "UPDATE book_jobs SET stage = 'ready', state = 'done',"
           " stopped_reason = NULL, resumable_at = NULL, updated_at = ?"
           " WHERE book_id = ?", (_iso(_aware(now)), book_id))


def is_fresh(job: BookJob, now: datetime) -> bool:
    return job.state == RUNNING and _aware(now) - job.updated_at < STALE_AFTER


def recover_stale(conn: sqlite3.Connection, now: datetime | None = None,
                  max_age: timedelta = STALE_AFTER) -> list[str]:
    """Running rows with no heartbeat for `max_age` become idle: interrupted.

    Returns the recovered book ids. Continue resumes them with no reset wait.
    """
    moment = _aware(now)
    cutoff = _iso(moment - max_age)
    ids = [r[0] for r in conn.execute(
        "SELECT book_id FROM book_jobs WHERE state = 'running'"
        " AND updated_at <= ? ORDER BY book_id", (cutoff,))]
    with conn:
        for book_id in ids:
            conn.execute(
                "UPDATE book_jobs SET state = 'idle',"
                " stopped_reason = 'interrupted', updated_at = ?"
                " WHERE book_id = ?", (_iso(moment), book_id))
    return ids


def can_continue(job: BookJob | None, now: datetime | None = None) -> tuple[bool, str]:
    """Whether Continue (or Process, with no job yet) may start now, and why not."""
    moment = _aware(now)
    if job is None:
        return True, ""
    if job.state == RUNNING and is_fresh(job, moment):
        return False, "a job is already running for this book"
    if job.state == DONE:
        return False, "this book is already processed"
    if job.state == WAITING_QUOTA and job.resumable_at and moment < job.resumable_at:
        local = job.resumable_at.astimezone().strftime("%H:%M")
        return False, f"the Gemini allowance resets at {local} (your local time)"
    return True, ""
