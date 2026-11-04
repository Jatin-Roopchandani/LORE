"""`lore worker`: runs book-processing jobs from the central queue.

The web app (multi mode) enqueues a job per Continue or Move; this process
claims them (lore.central.queue: atomic, one live job per user, any number of
workers), runs each with the existing `run_book` pipeline against that user's
own library (`config_for(base, user)`), and reports progress where it always
has: the user's `book_jobs` row. The central row only says who holds the job
and whether the holder is alive.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from lore.central import queue
from lore.central.models import KIND_MOVE, Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs, runner
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.move import prepare_move
from lore.tenancy import config_for
from lore.worlds import get_world

log = logging.getLogger(__name__)

HEARTBEAT_EVERY = 30.0  # seconds; a claim goes stale after queue.STALE_AFTER (180 s)
POLL_EVERY = 2.0

# How a run that did not process anything ended: `skipped` is a job that was
# not needed or not allowed when the worker got to it (already done, a live
# runner, the allowance not yet reset, nothing to move).
SKIPPED = "skipped"


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    """One worker. `make_stages`, `make_move_stages` and `clock` are the test
    seams the web app has too."""

    def __init__(self, base: LoreConfig, sessions: sessionmaker[Session], *,
                 worker_id: str | None = None,
                 make_stages: Callable[[LoreConfig], list] = runner.default_stages,
                 make_move_stages: Callable[[LoreConfig, int], list] = runner.move_stages,
                 clock: Callable[[], datetime] | None = None,
                 stale_after: timedelta = queue.STALE_AFTER,
                 heartbeat_every: float = HEARTBEAT_EVERY) -> None:
        self.base = base
        self.sessions = sessions
        self.worker_id = worker_id or default_worker_id()
        self._make_stages = make_stages
        self._make_move_stages = make_move_stages
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stale_after = stale_after
        self._heartbeat_every = heartbeat_every

    # --- one pass ---------------------------------------------------------

    def run_once(self) -> Job | None:
        """Requeue abandoned jobs, claim one and run it. The finished job,
        or None when nothing was waiting."""
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
            outcome, failed, error = self._execute(job)
        except Exception as exc:  # noqa: BLE001 - recorded on the central row
            log.exception("job %s failed", job.id)
            outcome, failed, error = "failed", True, f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            beat.join()
        with self.sessions() as session:
            if not queue.finish(session, job.id, self.worker_id, outcome=outcome,
                                failed=failed, error=error, now=self._clock()):
                log.warning("job %s: claim was lost before it finished", job.id)

    def _beat(self, job_id: int, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_every):
            try:
                with self.sessions() as session:
                    queue.heartbeat(session, job_id, self.worker_id,
                                    now=self._clock())
            except Exception:  # noqa: BLE001 - the next beat tries again
                log.exception("heartbeat failed")

    def _execute(self, job: Job) -> tuple[str, bool, str | None]:
        """Prepare the user's `book_jobs` row, run the stages. Returns the
        central outcome, whether it failed, and the reason."""
        config = config_for(self.base, job.user_id)  # validates the id
        now = self._clock()
        conn = open_db(config.sqlite_path)
        try:
            jobs.recover_stale(conn, now)  # a crashed worker's row resumes
            if conn.execute("SELECT 1 FROM books WHERE id = ?",
                            (job.book_id,)).fetchone() is None:
                return "failed", True, f"no book with id {job.book_id!r}"
            move_world: int | None = None
            try:
                if job.kind == KIND_MOVE:
                    move_world = (job.payload or {}).get("world")
                    if move_world is None or get_world(conn, move_world) is None:
                        return "failed", True, f"no world {move_world!r}"
                    if not prepare_move(conn, job.book_id, move_world, now=now):
                        return SKIPPED, False, "nothing to move"
                else:
                    ok, why = jobs.can_continue(jobs.get_job(conn, job.book_id), now)
                    if not ok:
                        return SKIPPED, False, why
                    # An unfinished move continues as a move (see the web app).
                    move_world = jobs.move_target(conn, job.book_id)
                    if move_world is not None and get_world(conn, move_world) is None:
                        move_world = None
                    jobs.claim_job(conn, job.book_id, now=now)
            except JobRefused as exc:
                return SKIPPED, False, str(exc)
        finally:
            conn.close()
        result = runner.run_claimed(config, job.book_id, move_world, self._clock,
                                    make_stages=self._make_stages,
                                    make_move_stages=self._make_move_stages)
        if result is None:
            return SKIPPED, False, "another runner holds this book"
        if result.state == jobs.FAILED:
            return "failed", True, result.stopped_reason
        # done, or waiting_quota (the user continues after the reset)
        return result.state, False, result.stopped_reason
