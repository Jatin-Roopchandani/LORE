"""`lore worker`: runs book-processing jobs from the central queue.

The web app (multi mode) enqueues a job per Continue or Move; this process
claims them (lore.central.queue: atomic, one live job per user, any number of
workers), runs each with the existing `run_book` pipeline against that user's
own library (`config_for(base, user)`), and reports progress where it always
has: the user's `book_jobs` row. The central row only says who holds the job
and whether the holder is alive.

Keys (release step 3): each job runs with ITS user's own Gemini key, decrypted
from the central database and bound for the job (lore.inference.key_scope),
so every model client the stages build uses that key and that user's budget
(the ledger in their lore.db, and the central `usage` count). The server's key
file is never used. A user with no key, or a key Gemini refuses mid-job,
pauses that user's jobs (central `paused`, book row `waiting_key`) instead of
failing them; saving a working key queues them again.

A user who switches provider (or removes the key in use) while their book
processes: the job stops at its next safe point (pipeline.ProviderSwitch),
goes back to the queue and resumes where it stopped on the new provider; if
that one is paid and the estimate for the rest needs confirming, the book
waits for Continue instead.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy.orm import Session, sessionmaker

from lore.central import queue
from lore.central.models import KIND_MOVE, Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference import key_scope
from lore.inference.key_scope import KeyRequired, KeyScope
from lore.orchestrator import jobs, runner
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.move import prepare_move
from lore.inference.gemini import CREDIT_MESSAGE
from lore.orchestrator.pipeline import SWITCH_REASON, KeyStop
from lore.tenancy import config_for
from lore.worlds import get_world

if TYPE_CHECKING:
    from lore.central.keys import KeyService

log = logging.getLogger(__name__)

HEARTBEAT_EVERY = 30.0  # seconds; a claim goes stale after queue.STALE_AFTER (180 s)
POLL_EVERY = 2.0

# How a run that did not process anything ended: `skipped` is a job that was
# not needed or not allowed when the worker got to it (already done, a live
# runner, the allowance not yet reset, nothing to move).
SKIPPED = "skipped"

# Paused outcomes (the central job waits in `paused`, not failed).
KEY_REQUIRED = "key_required"   # the user has not saved a key
KEY_INVALID = "key_invalid"     # Gemini refused it (or it no longer decrypts)
NO_KEY_REASON = ("Paused: connect your AI provider's API key in Settings, then"
                 " this book continues.")
REJECTED_REASON = ("Paused: your AI provider refused your API key. Replace it in"
                   " Settings, then this book continues.")
# Gemini 402: the key's prepaid credit is used up (the key itself is fine).
CREDIT_EXHAUSTED = "credit_exhausted"
# The user switched to a paid provider mid-job and the rest needs confirming.
NEEDS_CONFIRM = "needs_confirm"


PURGE_EVERY = timedelta(hours=1)
# Past the book row's STALE_AFTER before a delayed job is claimed again.
BUSY_MARGIN = timedelta(seconds=5)


class Outcome(NamedTuple):
    outcome: str
    failed: bool = False
    error: str | None = None
    paused: bool = False
    requeue: bool = False  # stopped at a safe point: run it again (new provider)
    not_before: datetime | None = None  # with requeue: not claimed before this


class KeyGuard:
    """Wraps a stage: a missing or refused user key stops the run as KeyStop
    (the job pauses), however the stage reported the failure."""

    def __init__(self, stage, scope: KeyScope) -> None:
        self._stage, self._scope = stage, scope
        self.name = stage.name

    def __getattr__(self, name):  # report, skipped, ... of the wrapped stage
        return getattr(self._stage, name)

    def total(self, ctx) -> int:
        return self._stage.total(ctx)

    def run(self, ctx, job_ctl) -> None:
        try:
            self._stage.run(ctx, job_ctl)
        except KeyStop:
            raise
        except Exception as exc:
            if self._scope.rejected:
                raise KeyStop(REJECTED_REASON) from exc
            if _caused_by(exc, KeyRequired):
                raise KeyStop(NO_KEY_REASON) from exc
            raise


def _caused_by(exc: BaseException, kind: type) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kind):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    """One worker. `make_stages`, `make_move_stages` and `clock` are the test
    seams the web app has too. `keys` decrypts each job's user key (`lore
    worker` always passes one); without it every job runs with an empty key
    scope, so a stage that needs Gemini pauses rather than use any other key."""

    def __init__(self, base: LoreConfig, sessions: sessionmaker[Session], *,
                 keys: "KeyService | None" = None,
                 worker_id: str | None = None,
                 make_stages: Callable[[LoreConfig], list] = runner.default_stages,
                 make_move_stages: Callable[[LoreConfig, int], list] = runner.move_stages,
                 clock: Callable[[], datetime] | None = None,
                 stale_after: timedelta = queue.STALE_AFTER,
                 heartbeat_every: float = HEARTBEAT_EVERY) -> None:
        self.base = base
        self.sessions = sessions
        self.keys = keys
        self.worker_id = worker_id or default_worker_id()
        self._make_stages = make_stages
        self._make_move_stages = make_move_stages
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_after = stale_after
        self._heartbeat_every = heartbeat_every
        self._purged_at: datetime | None = None

    def purge_traces(self) -> int:
        """Delete traces past their retention, at most once an hour (one
        indexed delete); a failure is logged, never fatal."""
        now = self._clock()
        if self._purged_at is not None and now - self._purged_at < PURGE_EVERY:
            return 0
        self._purged_at = now
        try:
            from lore.central import traces
            with self.sessions() as session:
                gone = traces.purge_expired(session, now)
                session.commit()
            return gone
        except Exception:  # noqa: BLE001
            log.exception("purging expired traces failed")
            return 0

    # --- one pass ---------------------------------------------------------

    def run_once(self) -> Job | None:
        """Requeue abandoned jobs, claim one and run it. The finished job,
        or None when nothing was waiting."""
        self.purge_traces()
        with self.sessions() as session:
            queue.requeue_stale(session, now=self._clock(), max_age=self._stale_after)
            job = queue.claim_next(session, self.worker_id, now=self._clock())
        if job is None:
            return None
        self.run_job(job)
        with self.sessions() as session:
            return session.get(Job, job.id)

    def run_forever(self, stop: threading.Event | None = None,
                    poll_every: float = POLL_EVERY) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                ran = self.run_once()
            except Exception:  # noqa: BLE001 - a bad pass must not end the worker
                log.exception("worker pass failed")
                ran = None
            if ran is None:
                stop.wait(poll_every)

    # --- one job ----------------------------------------------------------

    def run_job(self, job: Job) -> None:
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(job.id, stop),
                                name="lore-worker-heartbeat", daemon=True)
        beat.start()
        try:
            result = self._execute(job)
        except Exception as exc:  # noqa: BLE001 - recorded on the central row
            log.exception("job %s failed", job.id)
            result = Outcome("failed", True, f"{type(exc).__name__}: {exc}")
        finally:
            stop.set()
            beat.join()
        try:
            if result.requeue:
                with self.sessions() as session:
                    if not queue.requeue(session, job.id, self.worker_id,
                                         not_before=result.not_before):
                        log.warning("job %s: claim was lost before it was requeued", job.id)
                return
            if result.paused:
                self._pause(job, result)
                return
            with self.sessions() as session:
                if not queue.finish(session, job.id, self.worker_id,
                                    outcome=result.outcome, failed=result.failed,
                                    error=result.error, now=self._clock()):
                    log.warning("job %s: claim was lost before it finished", job.id)
        finally:
            self._finish_deletion(job.user_id)

    def _finish_deletion(self, user_id: str) -> None:
        """An account deleted while this job ran left its library for us
        (lore.central.erase): remove it now that nothing holds it open."""
        try:
            from lore.central import erase
            if erase.finish_pending(self.sessions, self.base.data_dir, user_id):
                log.info("removed the library of deleted account %s", user_id)
        except Exception:  # noqa: BLE001 - logged; the next job's end tries again
            log.exception("finishing an account deletion failed")

    def _pause(self, job: Job, result: Outcome) -> None:
        """Park this job and the user's queued ones; a refused key is marked."""
        with self.sessions() as session:
            if not queue.pause(session, job.id, self.worker_id,
                               outcome=result.outcome, error=result.error):
                log.warning("job %s: claim was lost before it paused", job.id)
        if result.outcome == NEEDS_CONFIRM:
            return  # only this book waits for the user's Continue
        if result.outcome == KEY_INVALID and self.keys is not None:
            self.keys.mark_rejected(job.user_id)  # also pauses the queued ones
        else:
            with self.sessions() as session:
                if result.outcome == CREDIT_EXHAUSTED:
                    queue.pause_user(session, job.user_id, outcome=CREDIT_EXHAUSTED)
                else:
                    queue.pause_user(session, job.user_id)

    def _beat(self, job_id: int, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_every):
            try:
                with self.sessions() as session:
                    queue.heartbeat(session, job_id, self.worker_id,
                                    now=self._clock())
            except Exception:  # noqa: BLE001 - the next beat tries again
                log.exception("heartbeat failed")

    def _scope(self, user_id: str) -> KeyScope | Outcome:
        """This user's key, bound for the job; or the pause when there is none."""
        if self.keys is None:
            return KeyScope(user_id, None)
        from lore.central.keys import KeyDecryptError
        try:
            # The user's active provider (Gemini, Claude or OpenAI), its key,
            # model choice, usage counting and their own token cap.
            return self.keys.scope(user_id)
        except KeyRequired:
            return Outcome(KEY_REQUIRED, error=NO_KEY_REASON, paused=True)
        except KeyDecryptError:
            log.warning("a stored key did not decrypt (user %s)", user_id)
            return Outcome(KEY_INVALID, error=REJECTED_REASON, paused=True)

    def _busy(self, row: jobs.BookJob) -> Outcome:
        """The book still looks running (a stopped worker's fresh heartbeat,
        or a CLI run): give the job back until that row would go stale,
        rather than finish it as skipped and leave the book stopped."""
        when = row.updated_at + jobs.STALE_AFTER + BUSY_MARGIN
        return Outcome(SKIPPED, error="a job is already running for this book",
                       requeue=True, not_before=when)

    def _interrupted(self, job: Job, config: LoreConfig) -> None:
        """A graceful stop (Ctrl-C, SIGTERM) mid-job: the book row says
        interrupted (Continue needs no wait) and the job goes back to the
        queue, so the next worker resumes it at once. Best effort."""
        try:
            conn = open_db(config.sqlite_path)
            try:
                row = jobs.get_job(conn, job.book_id)
                if row is not None and row.state == jobs.RUNNING:
                    jobs.stop_switch(conn, job.book_id, "interrupted", now=self._clock())
            finally:
                conn.close()
            with self.sessions() as session:
                queue.requeue(session, job.id, self.worker_id)
        except Exception:  # noqa: BLE001 - stopping anyway; staleness recovers it
            log.exception("job %s: could not mark it interrupted", job.id)

    def _after_switch(self, job: Job, config: LoreConfig, row: jobs.BookJob) -> Outcome:
        """The user switched provider mid-job: requeue it to resume on the new
        one, unless that one is paid and the estimate for the rest needs
        their confirmation (then the book waits; Continue confirms)."""
        if self.keys is None:
            return Outcome(SKIPPED, requeue=True)
        from lore.inference.providers import estimate as estimates
        from lore.inference.providers.factory import label
        state = self.keys.providers(job.user_id)
        provider, model, paid = estimates.processing_model(state, config)
        if provider == "gemini" and not paid:
            return Outcome(SKIPPED, requeue=True)
        conn = open_db(config.sqlite_path)
        try:
            chunks = estimates.chunk_count(conn, job.book_id)
            left = estimates.remaining(chunks, row.stage, row.done_items, row.total_items,
                                       provider, model, paid)
            if not left["needs_confirm"]:
                return Outcome(SKIPPED, requeue=True)
            tokens = left["input_tokens"] + left["output_tokens"]
            usd = left["estimated_cost_usd"]
            reason = (f"Paused: you switched to {label(provider)}. The rest of this"
                      f" book should use about {tokens / 1e6:.1f} million tokens"
                      + (f", roughly ${usd:.2f}" if usd is not None else "")
                      + ", billed to your own account (an estimate). Continue to go on.")
            jobs.stop_switch(conn, job.book_id, reason, now=self._clock())
        finally:
            conn.close()
        return Outcome(NEEDS_CONFIRM, error=reason, paused=True)

    def _execute(self, job: Job) -> Outcome:
        """Prepare the user's `book_jobs` row, run the stages. Returns the
        central outcome, whether it failed or paused, and the reason."""
        config = config_for(self.base, job.user_id)  # validates the id
        scope = self._scope(job.user_id)
        if isinstance(scope, Outcome):
            return scope  # nothing touched: the book row is as it was
        now = self._clock()
        conn = open_db(config.sqlite_path)
        try:
            jobs.recover_stale(conn, now)  # a crashed worker's row resumes
            if conn.execute("SELECT 1 FROM books WHERE id = ?",
                            (job.book_id,)).fetchone() is None:
                return Outcome("failed", True, f"no book with id {job.book_id!r}")
            move_world: int | None = None
            try:
                if job.kind == KIND_MOVE:
                    move_world = (job.payload or {}).get("world")
                    if move_world is None or get_world(conn, move_world) is None:
                        return Outcome("failed", True, f"no world {move_world!r}")
                    if not prepare_move(conn, job.book_id, move_world, now=now):
                        return Outcome(SKIPPED, error="nothing to move")
                else:
                    current = jobs.get_job(conn, job.book_id)
                    if current is not None and jobs.is_fresh(current, now):
                        return self._busy(current)
                    ok, why = jobs.can_continue(current, now)
                    if not ok:
                        return Outcome(SKIPPED, error=why)
                    # An unfinished move continues as a move (see the web app).
                    move_world = jobs.move_target(conn, job.book_id)
                    if move_world is not None and get_world(conn, move_world) is None:
                        move_world = None
                    jobs.claim_job(conn, job.book_id, now=now)
            except JobRefused as exc:
                current = jobs.get_job(conn, job.book_id)
                if current is not None and jobs.is_fresh(current, self._clock()):
                    return self._busy(current)
                return Outcome(SKIPPED, error=str(exc))
        finally:
            conn.close()

        def guarded(stages: list) -> list:
            return [KeyGuard(stage, scope) for stage in stages]

        try:
            with key_scope.bound(scope):  # every client in the stages: this user's key
                result = runner.run_claimed(
                    config, job.book_id, move_world, self._clock,
                    make_stages=lambda cfg: guarded(self._make_stages(cfg)),
                    make_move_stages=lambda cfg, world: guarded(
                        self._make_move_stages(cfg, world)))
        except (KeyboardInterrupt, SystemExit):
            self._interrupted(job, config)
            raise
        if result is None:
            conn = open_db(config.sqlite_path)
            try:
                current = jobs.get_job(conn, job.book_id)
            finally:
                conn.close()
            if current is not None and jobs.is_fresh(current, self._clock()):
                return self._busy(current)
            return Outcome(SKIPPED, error="another runner holds this book")
        if result.state == jobs.IDLE and result.stopped_reason == SWITCH_REASON:
            return self._after_switch(job, config, result)
        if result.state == jobs.WAITING_KEY and result.stopped_reason == CREDIT_MESSAGE:
            return Outcome(CREDIT_EXHAUSTED, error=CREDIT_MESSAGE, paused=True)
        if result.state == jobs.WAITING_KEY:
            outcome = KEY_INVALID if scope.rejected else KEY_REQUIRED
            return Outcome(outcome, error=result.stopped_reason, paused=True)
        if result.state == jobs.FAILED:
            return Outcome("failed", True, result.stopped_reason)
        # done, or waiting_quota (the user continues after the reset)
        return Outcome(result.state, error=result.stopped_reason)
