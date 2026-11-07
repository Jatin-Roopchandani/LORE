"""The stage runner: drives one book through its stages, saving where it is.

A stage declares its items and skips the ones already done, so running it
again after a stop does only the missing work. The runner owns the
`book_jobs` row (see jobs.py): a QuotaStop parks the job as waiting_quota
until the daily allowance resets; Gemini's 402 (prepaid credit used up, from
any stage) parks it as waiting_key with a friendly reason, resumable with
Continue; any other exception marks it failed.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.gemini import CREDIT_MESSAGE, GeminiCreditError, limits_for
from lore.inference.providers import factory as providers
from lore.orchestrator import jobs
from lore.orchestrator.gemini_budget import calls_today, today

# Order matters. `beats` needs the loaded graph (its entities), so it follows load;
# `codex` profiles entities from their beats and passages, so it follows beats.
# `remove` and `reassign` belong to moving a book to another world (see
# move.py): take it out of the graph, change its world, then load and
# beats as usual. Only a move runs them, ahead of `load`.
STAGES = ("chunk", "extract", "remove", "reassign", "load", "disambiguate",
          "beats", "codex", "ready")


class QuotaStop(Exception):
    """The daily Gemini allowance (or the answer reserve) is reached.

    Raised by a stage; the runner parks the job as waiting_quota.
    """


class KeyStop(Exception):
    """The user's Gemini key is missing or Gemini refused it (multi mode).

    Raised by the worker's stage guard (lore.worker); the runner parks the
    job as waiting_key, resumable once a working key is saved. Local mode
    never raises it.
    """


class ProviderSwitch(QuotaStop):
    """The user switched provider (or removed the key in use) mid-job.

    Raised at a safe point (JobCtl.spend_check, before a request, between
    items); the runner parks the job as idle with SWITCH_REASON, and the
    worker requeues it to resume on the new provider. A QuotaStop so every
    path that halts cleanly on a spent allowance (extraction never retries
    or dead-letters on one) halts the same way on this.
    """


SWITCH_REASON = "Paused: you switched AI provider. Continuing with your new choice."

JobRefused = jobs.JobRefused  # run_book will not start (see jobs.py)


def caused_by(exc: BaseException | None, kind: type) -> bool:
    """True when `exc` or anything in its cause/context chain is a `kind`."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kind):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


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


Clock = Callable[[], datetime]


def wall_clock() -> datetime:
    return datetime.now(timezone.utc)


def _db_path(conn: sqlite3.Connection) -> str:
    return next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")


@contextmanager
def keep_alive(conn: sqlite3.Connection, book_id: str, clock: Clock = wall_clock,
               interval: float = 30.0) -> Iterator[None]:
    """Heartbeat the job row every `interval` seconds while the body runs.

    A daemon thread with its OWN connection (sqlite connections are not
    shared across threads). Stopped and joined on exit, whatever the exit:
    success, QuotaStop or failure, so it never writes after the stage.
    """
    stop = threading.Event()
    path = _db_path(conn)

    def beat() -> None:
        own = sqlite3.connect(path, timeout=5)
        try:
            while not stop.wait(interval):
                try:
                    with own:
                        own.execute(
                            "UPDATE book_jobs SET updated_at = ?"
                            " WHERE book_id = ? AND state = 'running'",
                            (jobs._iso(jobs._aware(clock())), book_id))
                except sqlite3.Error:
                    pass  # a busy database: the next beat tries again
        finally:
            own.close()

    thread = threading.Thread(target=beat, name="lore-keepalive", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


class JobCtl:
    """What a running stage uses to report progress and check the budget.

    `clock` is read on every call, so a long run keeps stamping fresh times.
    """

    def __init__(self, ctx: StageContext, clock: Clock = wall_clock) -> None:
        self._ctx = ctx
        self._clock = clock

    def _moment(self) -> datetime:
        return self._clock()

    def advance(self, done: int) -> None:
        jobs.advance(self._ctx.conn, self._ctx.book_id, done, now=self._moment())

    def heartbeat(self) -> None:
        jobs.heartbeat(self._ctx.conn, self._ctx.book_id, now=self._moment())

    def progress(self, done: int, total: int) -> None:
        """Both counts, for a stage that learns its total once under way."""
        jobs.set_progress(self._ctx.conn, self._ctx.book_id, done, total,
                          now=self._moment())

    def spend_check(self) -> None:
        """Call before each Gemini request; stops once only the answer
        reserve (or less) of today's requests remains."""
        cfg = self._ctx.config
        if key_scope.provider_changed():
            raise ProviderSwitch(SWITCH_REASON)
        if not providers.on_gemini(cfg):
            # Claude, OpenAI or a paid Gemini plan: billed to the user, so no
            # free-tier ledger and no reserve; only the user's own daily token
            # cap stops processing.
            if key_scope.over_token_cap():
                raise QuotaStop("your daily token cap is reached (Account settings);"
                                " processing continues after midnight Pacific")
            return
        left = processing_budget_left(self._ctx.conn, cfg, today(self._moment()))
        reserve = cfg.processing.answer_reserve
        if left <= reserve:
            raise QuotaStop(
                f"Gemini processing reserve reached ({left} requests left today,"
                f" {reserve} held back for answering questions)")


def _next_stage(name: str) -> str:
    return STAGES[min(STAGES.index(name) + 1, len(STAGES) - 1)]


def run_book(conn: sqlite3.Connection, config: LoreConfig, book_id: str,
             stages: list[Stage], *, clock: Clock | None = None,
             now: datetime | None = None, force: bool = False,
             claimed: bool = False,
             keepalive_interval: float = 30.0) -> jobs.BookJob:
    """Run `stages` (in the given order) for one book, resuming a saved job.

    `clock` is a callable read on every stamp (default: the wall clock);
    `now` is shorthand for a fixed clock, for tests. `claimed` means the
    caller already marked the row running (jobs.claim_job) and owns it.

    Returns the final job row (done, waiting_quota or failed). Raises
    JobRefused, leaving the row untouched, when a fresh `running` row exists
    (never overridden, even by `force`) or when the allowance has not reset
    yet (`force` overrides that one).
    """
    if clock is None:
        clock = (lambda: now) if now is not None else wall_clock
    job = jobs.get_job(conn, book_id)
    if not claimed:
        if job is not None and jobs.is_fresh(job, clock()):
            raise JobRefused("a job is already running for this book")
        if (job is not None and job.state == jobs.WAITING_QUOTA and not force):
            ok, why = jobs.can_continue(job, clock())
            if not ok:
                raise JobRefused(why)

    # Resume at the saved stage; a finished or absent job starts over, which
    # is safe because every stage skips what it has already done.
    resume_at = 0
    if job is not None and job.state != jobs.DONE and job.stage in STAGES:
        resume_at = STAGES.index(job.stage)

    # Claim the row once, atomically, and own it until the run ends: every
    # later stamp is unguarded, and the row stays `running` between stages.
    if not claimed:
        jobs.claim_job(conn, book_id, now=clock())
    jobs.set_provider(conn, book_id, providers.active_provider(config))

    ctx = StageContext(conn, config, book_id)
    ctl = JobCtl(ctx, clock)
    for stage in stages:
        if STAGES.index(stage.name) < resume_at:
            continue
        total = stage.total(ctx)
        jobs.start_job(conn, book_id, stage.name, total, now=clock(), guard=False)
        try:
            with keep_alive(conn, book_id, clock, keepalive_interval):
                stage.run(ctx, ctl)
        except KeyStop as stop:
            jobs.stop_key(conn, book_id, str(stop), now=clock())
            return jobs.get_job(conn, book_id)
        except Exception as exc:  # noqa: BLE001 - sorted below
            if caused_by(exc, ProviderSwitch):
                jobs.stop_switch(conn, book_id, SWITCH_REASON, now=clock())
                return jobs.get_job(conn, book_id)
            if caused_by(exc, GeminiCreditError):
                # Gemini 402: the key's prepaid credit is used up. Paused with
                # a fixed, friendly reason (never Google's JSON); Continue
                # resumes it once topped up, or on another provider.
                jobs.stop_key(conn, book_id, CREDIT_MESSAGE, now=clock())
                return jobs.get_job(conn, book_id)
            if not isinstance(exc, QuotaStop):
                jobs.fail(conn, book_id, f"{type(exc).__name__}: {exc}"[:4000],
                          now=clock())
                return jobs.get_job(conn, book_id)
            stop = exc
            cur = jobs.get_job(conn, book_id)
            jobs.stop_quota(
                conn, book_id,
                f"{stop} (during {stage.name}: {cur.done_items}/{cur.total_items})",
                now=clock())
            return jobs.get_job(conn, book_id)
        jobs.finish_stage(conn, book_id, _next_stage(stage.name), now=clock(),
                          keep_running=True)
    jobs.done(conn, book_id, now=clock())
    return jobs.get_job(conn, book_id)
