"""Multi mode: Continue and Move enqueue a central job and answer 202; the
worker, not a thread of the web process, runs it. Local mode is covered by
test_web_books.py and test_web_move.py, which still pass unchanged."""
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central.models import Job
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.tenancy import config_for
from lore.web.app import create_app
from lore.worker import Worker
from lore.worlds import get_world, list_worlds
from tests.central_fixtures import central_sessions
from tests.auth_fixtures import ACT_AS as HEADER, ActAsClient, make_app
from tests.test_worker import FakeStage, book_job, library

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    base = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    library(base, "alice", "b1")
    library(base, "bob", "b1", "b2")
    calls = []

    def no_threads(cfg):  # processing must never start in the web process
        calls.append(cfg.data_dir)
        return []

    app = make_app(base, sessions, stages_factory=no_threads,
                   move_stages_factory=lambda c, w: no_threads(c),
                   clock=lambda: NOW)
    return (ActAsClient(app, sessions), base, sessions, calls)


def central_jobs(sessions):
    with sessions() as s:
        return s.scalars(sa.select(Job).order_by(Job.id)).all()


def test_continue_enqueues_and_returns_202(env):
    client, base, sessions, calls = env
    r = client.post("/api/books/b1/continue", headers={HEADER: "alice"})
    assert r.status_code == 202
    body = r.json()
    assert body["started"] is True and body["queued"] is True
    (job,) = central_jobs(sessions)
    assert (job.id, job.user_id, job.book_id, job.kind, job.state) == (
        body["job"], "alice", "b1", "process", "queued")
    assert calls == []                                  # no stage factory was even asked
    assert book_job(base, "alice", "b1") is None        # nothing ran or claimed


def test_continue_twice_queues_one_job(env):
    client, _, sessions, _ = env
    a = client.post("/api/books/b1/continue", headers={HEADER: "alice"}).json()
    b = client.post("/api/books/b1/continue", headers={HEADER: "alice"})
    assert b.status_code == 202 and b.json()["job"] == a["job"]
    assert len(central_jobs(sessions)) == 1


def test_two_users_can_queue_the_same_book_id(env):
    client, _, sessions, _ = env
    client.post("/api/books/b1/continue", headers={HEADER: "alice"})
    client.post("/api/books/b1/continue", headers={HEADER: "bob"})
    assert [(j.user_id, j.book_id) for j in central_jobs(sessions)] == [
        ("alice", "b1"), ("bob", "b1")]


def test_a_user_cannot_queue_another_users_book(env):
    client, _, sessions, _ = env
    r = client.post("/api/books/b2/continue", headers={HEADER: "alice"})  # bob's
    assert r.status_code == 404 and central_jobs(sessions) == []


def test_continue_refuses_what_local_mode_refuses(env):
    client, base, sessions, _ = env
    conn = open_db(config_for(base, "alice").sqlite_path)
    jobs.start_job(conn, "b1", "codex", 1, now=datetime.now(timezone.utc))
    jobs.done(conn, "b1")
    conn.close()
    r = client.post("/api/books/b1/continue", headers={HEADER: "alice"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_resumable"
    assert central_jobs(sessions) == []


def test_move_enqueues_with_the_target_world(env):
    client, base, sessions, calls = env
    r = client.post("/api/books/b1/move", headers={HEADER: "alice"},
                    json={"world": "Baker Street", "create": True})
    assert r.status_code == 202
    body = r.json()
    (job,) = central_jobs(sessions)
    assert (job.kind, job.payload, job.user_id) == (
        "move", {"world": body["world"]["id"]}, "alice")
    conn = open_db(config_for(base, "alice").sqlite_path)
    assert [w.name for w in list_worlds(conn)] == ["Baker Street"]
    conn.close()
    assert calls == []


def test_a_move_cannot_ride_on_a_queued_process_job(env):
    client, base, sessions, _ = env
    client.post("/api/books/b1/continue", headers={HEADER: "alice"})
    r = client.post("/api/books/b1/move", headers={HEADER: "alice"},
                    json={"world": "Fresh", "create": True})
    assert r.status_code == 409
    conn = open_db(config_for(base, "alice").sqlite_path)
    assert list_worlds(conn) == []  # no stray world left behind
    conn.close()
    assert [j.kind for j in central_jobs(sessions)] == ["process"]


def test_an_unknown_user_cannot_queue(env):
    client, base, sessions, _ = env
    library(base, "mallory", "b1")  # a library, but no row in the central users table
    r = client.post("/api/books/b1/continue", headers={HEADER: "mallory"})
    assert r.status_code == 401 and central_jobs(sessions) == []


def test_the_worker_then_runs_it_in_the_users_library(env):
    client, base, sessions, _ = env
    client.post("/api/books/b1/continue", headers={HEADER: "bob"})
    ran = []
    worker = Worker(base, sessions, worker_id="w1",
                    make_stages=lambda cfg: [FakeStage("chunk", ran)],
                    clock=lambda: NOW)
    worker.run_once()
    assert ran == [("bob", "b1", "chunk")]
    shown = client.get("/api/books", headers={HEADER: "bob"}).json()["books"]
    assert {b["id"]: b["job"]["state"] if b["job"] else None for b in shown} == {
        "b1": "done", "b2": None}
    alice = client.get("/api/books", headers={HEADER: "alice"}).json()["books"]
    assert [b["job"] for b in alice] == [None]


def test_the_move_through_the_worker(env):
    client, base, sessions, _ = env
    r = client.post("/api/books/b1/move", headers={HEADER: "alice"},
                    json={"world": "W", "create": True}).json()
    ran = []
    Worker(base, sessions, worker_id="w1", clock=lambda: NOW,
           make_move_stages=lambda cfg, w: [FakeStage("remove", ran),
                                            FakeStage("load", ran)]).run_once()
    assert [x[2] for x in ran] == ["remove", "load"]
    assert book_job(base, "alice", "b1").state == "done"
    conn = open_db(config_for(base, "alice").sqlite_path)
    assert get_world(conn, r["world"]["id"]) is not None
    conn.close()
