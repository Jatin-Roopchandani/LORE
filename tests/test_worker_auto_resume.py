"""Paused books resume on their own (multi mode): a book paused until a time
(the daily reset, a few minutes after its provider could not be reached)
keeps its central job queued with `not_before` at that time; a worker claims
it once it is due. A missing key still waits for the user; Continue still
means "now"; a book paused by an older worker is woken by the sweep. No
network: fake stages and a fake clock."""
from datetime import timedelta

import pytest

from lore.central import queue
from lore.central.models import Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference.gemini import GeminiTransportError
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import FIRST_SHORT_PAUSE, QuotaStop
from lore.tenancy import config_for
from tests.central_fixtures import central_sessions
from tests.test_worker import NOW, book_job, enqueue, library, make_worker


@pytest.fixture
def base(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def sessions(tmp_path):
    return central_sessions(tmp_path, users=("alice", "bob"))[0]


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Stage:
    """One stage that raises `errors` in turn (None: it succeeds)."""

    name = "chunk"

    def __init__(self, *errors):
        self.errors, self.runs = list(errors), 0

    def total(self, ctx):
        return 1

    def run(self, ctx, job_ctl):
        self.runs += 1
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        job_ctl.advance(1)


def _central(sessions, job_id):
    with sessions() as db:
        return db.get(Job, job_id)


def test_a_book_paused_for_the_daily_reset_resumes_on_its_own(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    stage = Stage(QuotaStop("Gemini processing reserve reached"))
    clock = Clock(NOW)
    worker = make_worker(base, sessions, [stage], clock=clock)

    worker.run_once()
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.WAITING_QUOTA and row.auto_resumes
    central = _central(sessions, job.id)
    assert central.state == "queued" and central.not_before == row.resumable_at
    assert central.attempts == 0  # a pause is not a failed attempt

    clock.now = row.resumable_at - timedelta(minutes=1)
    assert worker.run_once() is None  # not before its time
    assert stage.runs == 1

    clock.now = row.resumable_at + timedelta(seconds=1)
    done = worker.run_once()
    assert done.id == job.id and done.state == "done"
    assert stage.runs == 2 and book_job(base, "alice", "b1").state == jobs.DONE


def test_an_unreachable_provider_pauses_for_minutes_then_resumes(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    stage = Stage(GeminiTransportError("could not reach the Gemini API"))
    clock = Clock(NOW)
    worker = make_worker(base, sessions, [stage], clock=clock)

    worker.run_once()
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.WAITING_QUOTA and row.short_pause
    assert row.resumable_at == NOW + FIRST_SHORT_PAUSE
    assert _central(sessions, job.id).not_before == row.resumable_at

    clock.now = row.resumable_at
    assert worker.run_once().state == "done"


def test_continue_during_a_short_pause_runs_now(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    clock = Clock(NOW)
    worker = make_worker(base, sessions, [Stage(GeminiTransportError("down"))], clock=clock)
    worker.run_once()
    with sessions() as db:  # what POST /continue does
        again, created = queue.enqueue(db, "alice", "b1", "process")
    assert (again.id, created, again.not_before) == (job.id, False, None)
    clock.now = NOW + timedelta(seconds=10)
    assert worker.run_once().state == "done"


def test_a_missing_key_still_waits_for_the_user(base, sessions):
    from lore.worker import NO_KEY_REASON
    from lore.orchestrator.pipeline import KeyStop
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    make_worker(base, sessions, [Stage(KeyStop(NO_KEY_REASON))]).run_once()
    assert _central(sessions, job.id).state == "paused"
    assert book_job(base, "alice", "b1").state == jobs.WAITING_KEY


def test_two_workers_never_run_the_same_book_at_once(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    clock = Clock(NOW)
    make_worker(base, sessions, [Stage(QuotaStop("spent"))], clock=clock).run_once()
    # Continue while it waits finds the same live job: still one per book.
    with sessions() as db:
        assert queue.enqueue(db, "alice", "b1", "process")[0].id == job.id
        assert db.query(Job).filter(Job.book_id == "b1").count() == 1


def test_a_book_paused_by_an_older_worker_is_woken_by_the_sweep(base, sessions):
    # The hosted reader's book: its job finished as `waiting_quota` (the old
    # worker's way) and nothing would ever wake it.
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.claim_job(conn, "b1", now=NOW - timedelta(hours=20))
    jobs.stop_quota(conn, "b1", "every configured model refused the request",
                    now=NOW - timedelta(hours=20))
    conn.close()
    old = enqueue(sessions, "alice", "b1")
    with sessions() as db:
        claimed = queue.claim_next(db, "old-worker", now=NOW - timedelta(hours=20))
        queue.finish(db, claimed.id, "old-worker", outcome="waiting_quota",
                     error="every configured model refused the request")
    assert book_job(base, "alice", "b1").resumable_at < NOW  # long past due

    ran = Stage()
    clock = Clock(NOW)
    worker = make_worker(base, sessions, [ran], clock=clock)
    done = worker.run_once()
    assert done is not None and done.id != old.id and done.state == "done"
    assert ran.runs == 1 and book_job(base, "alice", "b1").state == jobs.DONE
    # Swept once; the next pass within WAKE_EVERY does not sweep again.
    assert worker.wake_waiting() == []


def test_the_sweep_puts_a_not_yet_due_book_back_until_its_time(base, sessions):
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.claim_job(conn, "b1", now=NOW)
    jobs.stop_quota(conn, "b1", "spent", now=NOW)
    resume = jobs.get_job(conn, "b1").resumable_at
    conn.close()
    enqueue(sessions, "alice", "b1")
    with sessions() as db:
        claimed = queue.claim_next(db, "old", now=NOW)
        queue.finish(db, claimed.id, "old", outcome="waiting_quota")
    ran = Stage()
    worker = make_worker(base, sessions, [ran], clock=Clock(NOW + timedelta(hours=1)))
    woken = worker.run_once()
    assert woken.state == "queued" and woken.not_before == resume
    assert ran.runs == 0


def test_the_sweep_leaves_finished_skipped_and_live_books_alone(sessions):
    with sessions() as db:
        for book, outcome in (("done", "done"), ("skip", "skipped"),
                              ("idle", "idle")):
            job, _ = queue.enqueue(db, "alice", book, "process")
            queue.claim_next(db, "w", now=NOW)
            queue.finish(db, job.id, "w", outcome=outcome)
        live, _ = queue.enqueue(db, "bob", "b1", "process")
        assert queue.wake_waiting(db) == []


def test_a_waiting_book_is_not_running_so_it_can_be_deleted(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    make_worker(base, sessions, [Stage(QuotaStop("spent"))]).run_once()
    with sessions() as db:
        assert queue.running_books(db, "alice", now=NOW) == set()
        due = book_job(base, "alice", "b1").resumable_at
        assert queue.running_books(db, "alice", now=due) == {"b1"}
