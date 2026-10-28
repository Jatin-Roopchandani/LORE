"""The stage runner: drives one book through its stages, saving where it is.

A stage declares its items and skips the ones already done, so running it
again after a stop does only the missing work. The runner owns the
`book_jobs` row (see jobs.py): a QuotaStop parks the job as waiting_quota
until the daily allowance resets; any other exception marks it failed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from lore.config import LoreConfig
from lore.inference.gemini import limits_for
from lore.orchestrator import jobs
from lore.orchestrator.gemini_budget import calls_today, today

# Order matters. `beats` is a no-op placeholder until the story-beats spec.
STAGES = ("chunk", "extract", "load", "disambiguate", "beats", "ready")


class QuotaStop(Exception):
    """The daily Gemini allowance (or the answer reserve) is reached.

    Raised by a stage; the runner parks the job as waiting_quota.
    """


class JobRefused(Exception):
    """run_book will not start: a live runner, or the allowance has not reset."""


@dataclass
class StageContext:
    conn: sqlite3.Connection
    config: LoreConfig
    book_id: str


class Stage(Protocol):
    name: str

    def total(self, ctx: StageContext) -> int: ...

    def run(self, ctx: StageContext, job_ctl: "JobCtl") -> None: ...


def processing_budget_left(conn: sqlite3.Connection, config: LoreConfig,
                           day: str) -> int:
    """Requests left today across the extraction cascade, from the ledger."""
    models = dict.fromkeys(config.extraction.gemini_models)
    return sum(max(0, limits_for(m).rpd - calls_today(conn, m, day))
               for m in models)


class JobCtl:
    """What a running stage uses to report progress and check the budget."""

    def __init__(self, ctx: StageContext, now: datetime | None = None) -> None:
        self._ctx = ctx
        self._now = now

    def _moment(self) -> datetime:
        return self._now if self._now is not None else datetime.now(timezone.utc)

    def advance(self, done: int) -> None:
        jobs.advance(self._ctx.conn, self._ctx.book_id, done, now=self._now)

    def heartbeat(self) -> None:
        jobs.heartbeat(self._ctx.conn, self._ctx.book_id, now=self._now)

    def spend_check(self) -> None:
        """Call before each Gemini request; stops once only the answer
        reserve (or less) of today's requests remains."""
        cfg = self._ctx.config
        left = processing_budget_left(self._ctx.conn, cfg, today(self._moment()))
        reserve = cfg.processing.answer_reserve
        if left <= reserve:
            raise QuotaStop(
                f"Gemini processing reserve reached ({left} requests left today,"
                f" {reserve} held back for answering questions)")


def _next_stage(name: str) -> str:
    return STAGES[min(STAGES.index(name) + 1, len(STAGES) - 1)]


def run_book(conn: sqlite3.Connection, config: LoreConfig, book_id: str,
             stages: list[Stage], *, now: datetime | None = None,
             force: bool = False) -> jobs.BookJob:
    """Run `stages` (in the given order) for one book, resuming a saved job.

    Returns the final job row (done, waiting_quota or failed). Raises
    JobRefused, leaving the row untouched, when a fresh `running` row exists
    (never overridden, even by `force`) or when the allowance has not reset
    yet (`force` overrides that one).
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    job = jobs.get_job(conn, book_id)
    if job is not None and jobs.is_fresh(job, moment):
        raise JobRefused("a job is already running for this book")
    if (job is not None and job.state == jobs.WAITING_QUOTA and not force):
        ok, why = jobs.can_continue(job, moment)
        if not ok:
            raise JobRefused(why)

    # Resume at the saved stage; a finished or absent job starts over, which
    # is safe because every stage skips what it has already done.
    resume_at = 0
    if job is not None and job.state != jobs.DONE and job.stage in STAGES:
        resume_at = STAGES.index(job.stage)

    ctx = StageContext(conn, config, book_id)
    ctl = JobCtl(ctx, now)
    for stage in stages:
        if STAGES.index(stage.name) < resume_at:
            continue
        total = stage.total(ctx)
        jobs.start_job(conn, book_id, stage.name, total, now=now)
        try:
            stage.run(ctx, ctl)
        except QuotaStop as stop:
            cur = jobs.get_job(conn, book_id)
            jobs.stop_quota(
                conn, book_id,
                f"{stop} (during {stage.name}: {cur.done_items}/{cur.total_items})",
                now=moment)
            return jobs.get_job(conn, book_id)
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            jobs.fail(conn, book_id, f"{type(exc).__name__}: {exc}"[:4000],
                      now=now)
            return jobs.get_job(conn, book_id)
        jobs.finish_stage(conn, book_id, _next_stage(stage.name), now=now)
    jobs.done(conn, book_id, now=now)
    return jobs.get_job(conn, book_id)
