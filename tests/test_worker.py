"""`lore worker`: claims queued jobs and runs each against its owner's own
library, with fake stages (no Gemini, no network)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from lore.central import queue
from lore.central.models import Job, User
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import QuotaStop
from lore.tenancy import config_for
from lore.worker import Worker
from lore.worlds import create_world, world_of
from tests.central_fixtures import central_sessions

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class FakeStage:
    def __init__(self, name, ran=None, gate=None, started=None, boom=None):
        self.name, self._ran, self._gate = name, ran, gate
        self._started, self._boom = started, boom

    def total(self, ctx):
        return 1

    def run(self, ctx, job_ctl):
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            assert self._gate.wait(30)
        if self._boom is not None:
            raise self._boom
        job_ctl.advance(1)
        if self._ran is not None:
            self._ran.append((ctx.config.data_dir.name, ctx.book_id, self.name))


@pytest.fixture
def base(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def sessions(tmp_path):
    return central_sessions(tmp_path, users=("alice", "bob"))[0]


def library(base, user, *book_ids):
    conn = open_db(config_for(base, user).sqlite_path)
    for b in book_ids:
        conn.execute("INSERT INTO books (id, title, file_path) VALUES (?, ?, 'x')",
                     (b, f"Book {b}"))
    conn.commit()
    conn.close()


def book_job(base, user, book):
    conn = open_db(config_for(base, user).sqlite_path)
    try:
        return jobs.get_job(conn, book)
    finally:
        conn.close()


def book_status(base, user, book):
    conn = open_db(config_for(base, user).sqlite_path)
    try:
        return conn.execute("SELECT status FROM books WHERE id = ?", (book,)).fetchone()[0]
    finally:
        conn.close()


def make_worker(base, sessions, stages, **kw):
    return Worker(base, sessions, worker_id=kw.pop("worker_id", "w1"),
                  make_stages=lambda cfg: stages,
                  make_move_stages=kw.pop("move", lambda cfg, w: stages),
                  clock=kw.pop("clock", lambda: NOW), heartbeat_every=0.05, **kw)


def enqueue(sessions, user, book, kind="process", payload=None):
    with sessions() as s:
        return queue.enqueue(s, user, book, kind, payload)[0]


def test_enqueue_claim_run_done(base, sessions):
    library(base, "alice", "b1")
    ran = []
    job = enqueue(sessions, "alice", "b1")
    worker = make_worker(base, sessions, [FakeStage("chunk", ran), FakeStage("load", ran)])
    finished = worker.run_once()
    assert finished.id == job.id
    assert (finished.state, finished.outcome, finished.claimed_by) == ("done", "done", "w1")
    assert ran == [("alice", "b1", "chunk"), ("alice", "b1", "load")]
    assert book_job(base, "alice", "b1").state == "done"
    assert book_status(base, "alice", "b1") == "graphed"


def test_the_worker_uses_the_jobs_owners_library_only(base, sessions):
    library(base, "alice", "b1")
    library(base, "bob", "b1")  # same book id, different library
    ran = []
    enqueue(sessions, "bob", "b1")
    make_worker(base, sessions, [FakeStage("chunk", ran)]).run_once()
    assert ran == [("bob", "b1", "chunk")]
    assert book_job(base, "alice", "b1") is None
    assert book_job(base, "bob", "b1").state == "done"


def test_nothing_queued_is_none(base, sessions):
    assert make_worker(base, sessions, []).run_once() is None


def test_a_failing_stage_fails_the_job_with_the_reason(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    done = make_worker(base, sessions, [FakeStage("chunk", boom=RuntimeError("kaboom"))]).run_once()
    assert done.state == "failed" and "kaboom" in done.error
    assert book_job(base, "alice", "b1").state == "failed"


def test_a_quota_stop_is_a_finished_job_the_user_continues_later(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    done = make_worker(base, sessions, [FakeStage("chunk", boom=QuotaStop("spent"))]).run_once()
    assert (done.state, done.outcome) == ("done", "waiting_quota")
    assert "spent" in done.error
    assert book_job(base, "alice", "b1").state == "waiting_quota"


def test_a_book_already_processed_is_skipped(base, sessions):
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.start_job(conn, "b1", "codex", 1, now=NOW)
    jobs.done(conn, "b1", now=NOW)
    conn.close()
    ran = []
    enqueue(sessions, "alice", "b1")
    done = make_worker(base, sessions, [FakeStage("chunk", ran)]).run_once()
    assert (done.state, done.outcome) == ("done", "skipped") and ran == []


def test_an_unknown_book_fails_the_job(base, sessions):
    library(base, "alice")
    enqueue(sessions, "alice", "ghost")
    done = make_worker(base, sessions, [FakeStage("chunk")]).run_once()
    assert done.state == "failed" and "ghost" in done.error


def test_an_unsafe_user_id_fails_the_job_and_creates_nothing(base, tmp_path):
    sessions, _ = central_sessions(tmp_path, users=())
    with sessions() as s:
        s.add(User(id="../escape", email="x@example.org"))
        s.commit()
    enqueue(sessions, "../escape", "b1")
    done = make_worker(base, sessions, [FakeStage("chunk")]).run_once()
    assert done.state == "failed" and "not a UUID or a safe slug" in done.error
    assert not base.data_dir.exists() or list(base.data_dir.iterdir()) == []


def test_a_move_runs_the_move_stages_and_changes_the_world(base, sessions):
    library(base, "alice", "b1")
    conn = open_db(config_for(base, "alice").sqlite_path)
    world = create_world(conn, "Baker Street")
    conn.close()
    ran = []
    seen = {}

    def move(cfg, world_id):
        seen["world"] = world_id
        return [FakeStage("remove", ran), FakeStage("load", ran)]

    enqueue(sessions, "alice", "b1", "move", {"world": world})
    done = make_worker(base, sessions, [], move=move).run_once()
    assert (done.state, done.outcome) == ("done", "done")
    assert seen["world"] == world
    assert [r[2] for r in ran] == ["remove", "load"]
    assert book_job(base, "alice", "b1").state == "done"


def test_a_move_to_a_missing_world_fails(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1", "move", {"world": 99})
    done = make_worker(base, sessions, []).run_once()
    assert done.state == "failed" and "99" in done.error


def test_a_dead_workers_job_is_requeued_and_finished_by_another(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    with sessions() as s:  # w1 claims it, starts, and dies: row running, no heartbeat
        queue.claim_next(s, "w1", now=NOW)
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.claim_job(conn, "b1", now=NOW)
    conn.close()
    later = NOW + timedelta(minutes=10)
    ran = []
    done = make_worker(base, sessions, [FakeStage("chunk", ran)], worker_id="w2",
                       clock=lambda: later).run_once()
    assert done.id == job.id and (done.state, done.claimed_by, done.attempts) == (
        "done", "w2", 2)
    assert ran == [("alice", "b1", "chunk")]
    assert book_job(base, "alice", "b1").state == "done"


def test_a_live_claim_is_not_stolen(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    with sessions() as s:
        queue.claim_next(s, "w1", now=NOW)
    ran = []
    soon = NOW + timedelta(seconds=30)
    assert make_worker(base, sessions, [FakeStage("chunk", ran)], worker_id="w2",
                       clock=lambda: soon).run_once() is None
    assert ran == []


def test_a_users_jobs_run_one_at_a_time_across_workers(base, sessions):
    library(base, "alice", "b1", "b2")
    library(base, "bob", "b3")
    enqueue(sessions, "alice", "b1")
    enqueue(sessions, "alice", "b2")
    enqueue(sessions, "bob", "b3")
    gate, started, ran = threading.Event(), threading.Event(), []
    slow = make_worker(base, sessions, [FakeStage("chunk", ran, gate, started)],
                       worker_id="slow")
    t = threading.Thread(target=slow.run_once)
    t.start()
    try:
        assert started.wait(30)  # alice's b1 is running
        other = make_worker(base, sessions, [FakeStage("chunk", ran)], worker_id="fast")
        first = other.run_once()   # must skip alice's b2, take bob's b3
        second = other.run_once()  # alice's b2 is still waiting for b1
        assert first.book_id == "b3" and second is None
        gate.set()
        t.join(30)
        third = other.run_once()   # b1 finished: alice's b2 may run now
        assert third.book_id == "b2"
    finally:
        gate.set()
        t.join(30)
    assert sorted(r[1] for r in ran) == ["b1", "b2", "b3"]


def test_the_heartbeat_keeps_a_long_job_alive(base, sessions):
    library(base, "alice", "b1")
    job = enqueue(sessions, "alice", "b1")
    stamps = []

    class Watcher(FakeStage):
        def run(self, ctx, job_ctl):
            import time
            with sessions() as s:
                first = s.get(Job, job.id).heartbeat_at
            time.sleep(0.4)
            with sessions() as s:
                stamps.extend([first, s.get(Job, job.id).heartbeat_at])

    # the real clock here: the beat stamps with it
    worker = Worker(base, sessions, worker_id="w1",
                    make_stages=lambda cfg: [Watcher("chunk")],
                    heartbeat_every=0.05)
    worker.run_once()
    assert stamps[1] > stamps[0]


def test_run_forever_stops_on_the_event(base, sessions):
    library(base, "alice", "b1")
    enqueue(sessions, "alice", "b1")
    stop = threading.Event()
    worker = make_worker(base, sessions, [FakeStage("chunk")])
    t = threading.Thread(target=worker.run_forever, args=(stop, 0.01))
    t.start()
    for _ in range(300):
        with sessions() as s:
            if s.get(Job, 1).state == "done":
                break
        stop.wait(0.05)
    stop.set()
    t.join(10)
    assert not t.is_alive()
    with sessions() as s:
        assert s.get(Job, 1).state == "done"
