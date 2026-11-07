"""A claimed job whose book still looks running (a stopped worker's fresh
book_jobs row, within STALE_AFTER) is requeued with a delay, never finished as
`skipped`; and a worker stopped gracefully marks its book interrupted and gives
its job back, so the next claim needs no wait. No network."""
from datetime import timedelta

import pytest

from lore.central import queue
from lore.central.models import Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.tenancy import config_for
from tests.central_fixtures import central_sessions
from tests.test_worker import NOW, FakeStage, book_job, enqueue, library, make_worker


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


def _fresh_running_row(base, user, book, at):
    conn = open_db(config_for(base, user).sqlite_path)
    jobs.claim_job(conn, book, now=at)
    conn.close()


def test_a_fresh_running_row_requeues_the_job_with_a_delay(base, sessions):
    library(base, "alice", "b1")
    _fresh_running_row(base, "alice", "b1", NOW - timedelta(seconds=30))
    job = enqueue(sessions, "alice", "b1")
    ran = []
    clock = Clock(NOW)
    worker = make_worker(base, sessions, [FakeStage("chunk", ran)], clock=clock)

    first = worker.run_once()
    assert first.id == job.id
    assert first.state == "queued" and first.outcome is None  # not done/skipped
    assert first.not_before == NOW - timedelta(seconds=30) + jobs.STALE_AFTER \
        + timedelta(seconds=5)
    assert ran == []
    assert worker.run_once() is None  # not claimable before its time

    clock.now = first.not_before + timedelta(seconds=1)  # the row is stale by now
    done = worker.run_once()
    assert done.id == job.id and done.state == "done"
    assert ran == [("alice", "b1", "chunk")]
    assert book_job(base, "alice", "b1").state == "done"


def test_a_delayed_job_does_not_hold_up_other_users(base, sessions):
    library(base, "alice", "b1")
    library(base, "bob", "b1")
    _fresh_running_row(base, "alice", "b1", NOW)
    enqueue(sessions, "alice", "b1")
    enqueue(sessions, "bob", "b1")
    ran = []
    worker = make_worker(base, sessions, [FakeStage("chunk", ran)])
    worker.run_once()  # alice: requeued with a delay
    assert worker.run_once().user_id == "bob"
    assert ran == [("bob", "b1", "chunk")]


def test_other_refusals_are_still_skipped(base, sessions):
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.start_job(conn, "b1", "codex", 1, now=NOW)
    jobs.done(conn, "b1", now=NOW)
    conn.close()
    enqueue(sessions, "alice", "b1")
    done = make_worker(base, sessions, [FakeStage("chunk")]).run_once()
    assert (done.state, done.outcome) == ("done", "skipped")


def test_a_graceful_stop_marks_the_book_interrupted_and_requeues(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    worker = make_worker(base, sessions, [FakeStage("chunk", boom=KeyboardInterrupt())])
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    row = book_job(base, "alice", "b1")
    assert row.state == jobs.IDLE and row.stopped_reason == "interrupted"
    with sessions() as db:
        back = db.get(Job, job.id)
        assert back.state == "queued" and back.claimed_by is None and back.not_before is None
    ran = []
    done = make_worker(base, sessions, [FakeStage("chunk", ran)]).run_once()
    assert done.state == "done" and ran == [("alice", "b1", "chunk")]


def test_requeue_gives_the_attempt_back(sessions):
    job = enqueue(sessions, "alice", "b1")
    with sessions() as db:
        claimed = queue.claim_next(db, "w1", now=NOW)
        assert claimed.attempts == 1
        assert queue.requeue(db, job.id, "w1", not_before=NOW + timedelta(minutes=2))
        assert queue.claim_next(db, "w1", now=NOW) is None
        again = queue.claim_next(db, "w1", now=NOW + timedelta(minutes=3))
        assert again.id == job.id and again.attempts == 1
        assert not queue.requeue(db, job.id, "someone-else")
