"""Moving a book between worlds from the web, and managing worlds.

Fake stages and a fake clock: no Gemini, no network, no graph work.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lore import worlds
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.web.app import create_app
from tests.worlds_fixtures import ALPHA, BETA, build_library

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class Fake:
    """A stage that records that it ran (and optionally blocks or fails)."""

    def __init__(self, name, ran, *, gate=None, boom=None, effect=None):
        self.name, self._ran, self._gate = name, ran, gate
        self._boom, self._effect = boom, effect

    def total(self, ctx):
        return 1

    def run(self, ctx, job_ctl):
        job_ctl.advance(0)
        if self._boom:
            raise RuntimeError(self._boom)
        self._ran.append((self.name, ctx.book_id))
        if self._effect:
            self._effect(ctx)
        if self._gate is not None:
            assert self._gate.wait(5)
        job_ctl.advance(1)


def _fake_move(ran, **flags):
    """Move stages as `move_stages` builds them, faked: reassign really
    reassigns (so the book's world changes), the rest only record."""
    def factory(config, world_id):
        return [
            Fake("remove", ran, boom=flags.get("remove_boom")),
            Fake("reassign", ran, effect=lambda ctx: worlds.assign(
                ctx.conn, ctx.book_id, world_id), boom=flags.get("reassign_boom")),
            Fake("load", ran, gate=flags.get("gate"), boom=flags.get("load_boom")),
            Fake("beats", ran),
        ]
    return factory


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


def _client(config, *, move=None, stages=None):
    app = create_app(config, stages_factory=lambda cfg: stages or [],
                     move_stages_factory=move, clock=lambda: NOW)
    return TestClient(app, base_url="http://127.0.0.1:8766")


def _db(config):
    return open_db(config.sqlite_path)


def _job(config, book):
    conn = _db(config)
    try:
        return jobs.get_job(conn, book)
    finally:
        conn.close()


def _world_of(config, book):
    conn = _db(config)
    try:
        return worlds.world_of(conn, book).name
    finally:
        conn.close()


def _wait(config, book, states=(jobs.DONE, jobs.FAILED)):
    for _ in range(300):
        job = _job(config, book)
        if job is not None and job.state in states:
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _wait_slot_free(client):
    for _ in range(200):
        r = client.post("/api/books/zzz/move", json={"world": ALPHA})
        if r.status_code != 409:
            return
        time.sleep(0.02)
    raise AssertionError("slot never freed")


# --- move ---------------------------------------------------------------

def test_move_runs_the_move_stages_and_returns_202(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran))
    r = client.post("/api/books/bb1/move", json={"world": ALPHA})
    assert r.status_code == 202
    assert r.json()["started"] is True
    assert _wait(config, "bb1").state == jobs.DONE
    assert [s for s, _ in ran] == ["remove", "reassign", "load", "beats"]
    assert _world_of(config, "bb1") == ALPHA


def test_move_by_world_id(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran))
    assert client.post("/api/books/bb1/move", json={"world": 1}).status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    assert _world_of(config, "bb1") == ALPHA


def test_move_is_marked_running_before_the_202(config) -> None:
    gate = threading.Event()
    client = _client(config, move=_fake_move([], gate=gate))
    try:
        assert client.post("/api/books/bb1/move",
                           json={"world": ALPHA}).status_code == 202
        assert _job(config, "bb1").state == jobs.RUNNING
    finally:
        gate.set()
    _wait(config, "bb1")


def test_move_while_busy_is_409(config) -> None:
    gate = threading.Event()
    client = _client(config, move=_fake_move([], gate=gate))
    try:
        assert client.post("/api/books/bb1/move",
                           json={"world": ALPHA}).status_code == 202
        r = client.post("/api/books/ba1/move", json={"world": BETA})
        assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
    finally:
        gate.set()
    _wait(config, "bb1")


def test_move_unknown_book_is_404(config) -> None:
    r = _client(config, move=_fake_move([])).post(
        "/api/books/nope/move", json={"world": ALPHA})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_move_to_an_unknown_world_is_400_and_creates_nothing(config) -> None:
    r = _client(config, move=_fake_move([])).post(
        "/api/books/bb1/move", json={"world": "Gamma"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_world"
    conn = _db(config)
    assert [w.name for w in worlds.list_worlds(conn)] == [ALPHA, BETA]
    conn.close()
    assert _job(config, "bb1") is None


def test_move_with_create_makes_the_world(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran))
    r = client.post("/api/books/bb1/move", json={"world": "Gamma", "create": True})
    assert r.status_code == 202
    assert r.json()["world"]["name"] == "Gamma"
    assert _wait(config, "bb1").state == jobs.DONE
    assert _world_of(config, "bb1") == "Gamma"


def test_move_with_create_and_a_blank_name_is_400(config) -> None:
    r = _client(config, move=_fake_move([])).post(
        "/api/books/bb1/move", json={"world": "  ", "create": True})
    assert r.status_code == 400


def test_a_refused_move_does_not_leave_a_created_world(config) -> None:
    """A job running elsewhere (a CLI run) refuses the move: no stray world."""
    conn = _db(config)
    jobs.start_job(conn, "bb1", "extract", 3, now=NOW)  # fresh running row
    conn.close()
    r = _client(config, move=_fake_move([])).post(
        "/api/books/bb1/move", json={"world": "Gamma", "create": True})
    assert r.status_code == 409
    conn = _db(config)
    assert [w.name for w in worlds.list_worlds(conn)] == [ALPHA, BETA]
    conn.close()


def test_move_to_the_current_world_is_a_no_op(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran))
    r = client.post("/api/books/bb1/move", json={"world": BETA})
    assert r.status_code == 200 and r.json() == {"started": False}
    assert ran == [] and _job(config, "bb1") is None


def test_a_move_failure_is_recorded_and_the_slot_frees(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran, load_boom="kaput"))
    assert client.post("/api/books/bb1/move", json={"world": ALPHA}).status_code == 202
    job = _wait(config, "bb1")
    assert job.state == jobs.FAILED and "kaput" in job.stopped_reason
    _wait_slot_free(client)


# --- Continue resumes a move ---------------------------------------------

def test_continue_after_an_interrupted_remove_runs_remove_and_reassign(config) -> None:
    """The move stopped in `remove`: Continue must not fall back to the
    ordinary stages (which would skip remove and reassign)."""
    ran: list = []
    client = _client(config, move=_fake_move(ran, remove_boom="interrupted"),
                     stages=[Fake("chunk", ran), Fake("load", ran)])
    assert client.post("/api/books/bb1/move", json={"world": ALPHA}).status_code == 202
    assert _wait(config, "bb1").state == jobs.FAILED
    assert _job(config, "bb1").stage == "remove"
    _wait_slot_free(client)

    ran.clear()
    client2 = _client(config, move=_fake_move(ran),
                      stages=[Fake("chunk", ran), Fake("load", ran)])
    r = client2.post("/api/books/bb1/continue")
    assert r.status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    assert [s for s, _ in ran] == ["remove", "reassign", "load", "beats"]
    assert _world_of(config, "bb1") == ALPHA


def test_continue_after_a_failed_reassign_reaches_the_target_world(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran, reassign_boom="x"))
    client.post("/api/books/bb1/move", json={"world": ALPHA})
    assert _wait(config, "bb1").state == jobs.FAILED
    assert _job(config, "bb1").stage == "reassign"
    assert _world_of(config, "bb1") == BETA
    _wait_slot_free(client)
    ran.clear()
    client2 = _client(config, move=_fake_move(ran))
    assert client2.post("/api/books/bb1/continue").status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    assert [s for s, _ in ran] == ["reassign", "load", "beats"]
    assert _world_of(config, "bb1") == ALPHA


def test_continue_after_a_move_stopped_in_load_uses_the_move_stages(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran, load_boom="quota"))
    client.post("/api/books/bb1/move", json={"world": ALPHA})
    assert _wait(config, "bb1").state == jobs.FAILED
    _wait_slot_free(client)
    ran.clear()
    client2 = _client(config, move=_fake_move(ran),
                      stages=[Fake("chunk", ran), Fake("load", ran)])
    assert client2.post("/api/books/bb1/continue").status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    assert [s for s, _ in ran] == ["load", "beats"]


def test_a_finished_move_leaves_continue_on_the_ordinary_stages(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran), stages=[Fake("chunk", ran)])
    client.post("/api/books/bb1/move", json={"world": ALPHA})
    assert _wait(config, "bb1").state == jobs.DONE
    conn = _db(config)
    assert jobs.move_target(conn, "bb1") is None
    conn.close()


def test_an_ordinary_job_continues_with_the_ordinary_stages(config) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran), stages=[Fake("chunk", ran)])
    assert client.post("/api/books/bb1/continue").status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    assert ran == [("chunk", "bb1")]


# --- status after a web load ---------------------------------------------

def test_status_is_graphed_after_a_web_run_completes(config) -> None:
    conn = _db(config)
    with conn:
        conn.execute("UPDATE books SET status = 'chunked' WHERE id = 'ba1'")
    conn.close()
    ran: list = []
    client = _client(config, stages=[Fake("load", ran)])
    assert client.post("/api/books/ba1/continue").status_code == 202
    assert _wait(config, "ba1").state == jobs.DONE
    books = {b["id"]: b for b in client.get("/api/books").json()["books"]}
    assert books["ba1"]["status"] == "graphed"


def test_status_is_not_graphed_after_a_failed_run(config) -> None:
    conn = _db(config)
    with conn:
        conn.execute("UPDATE books SET status = 'chunked' WHERE id = 'ba1'")
    conn.close()
    client = _client(config, stages=[Fake("load", [], boom="x")])
    client.post("/api/books/ba1/continue")
    assert _wait(config, "ba1").state == jobs.FAILED
    books = {b["id"]: b for b in client.get("/api/books").json()["books"]}
    assert books["ba1"]["status"] == "chunked"


def test_books_list_carries_each_books_world(config) -> None:
    books = {b["id"]: b for b in _client(config).get("/api/books").json()["books"]}
    assert books["ba1"]["world"] == ALPHA and books["bb1"]["world"] == BETA


# --- world management ----------------------------------------------------

def test_create_a_world(config) -> None:
    client = _client(config)
    r = client.post("/api/worlds", json={"name": " Gamma "})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Gamma" and isinstance(body["id"], int)
    names = [w["name"] for w in client.get("/api/worlds").json()]
    assert names == [ALPHA, BETA, "Gamma"]


def test_create_a_world_refuses_blank_and_duplicate(config) -> None:
    client = _client(config)
    assert client.post("/api/worlds", json={"name": " "}).status_code == 400
    r = client.post("/api/worlds", json={"name": ALPHA.lower()})
    assert r.status_code == 409 and r.json()["error"]["code"] == "exists"


def test_rename_a_world(config) -> None:
    client = _client(config)
    r = client.patch("/api/worlds/2", json={"name": "Beta Lands"})
    assert r.status_code == 200 and r.json() == {"id": 2, "name": "Beta Lands"}
    assert [w["name"] for w in client.get("/api/worlds").json()] == [ALPHA, "Beta Lands"]


def test_rename_errors(config) -> None:
    client = _client(config)
    assert client.patch("/api/worlds/99", json={"name": "X"}).status_code == 404
    assert client.patch("/api/worlds/2", json={"name": " "}).status_code == 400
    r = client.patch("/api/worlds/2", json={"name": ALPHA})
    assert r.status_code == 409 and r.json()["error"]["code"] == "exists"


def test_rename_to_its_own_name_with_other_case_is_allowed(config) -> None:
    r = _client(config).patch("/api/worlds/2", json={"name": BETA.upper()})
    assert r.status_code == 200 and r.json()["name"] == BETA.upper()


def test_delete_an_empty_world(config) -> None:
    client = _client(config)
    new_id = client.post("/api/worlds", json={"name": "Gamma"}).json()["id"]
    r = client.delete(f"/api/worlds/{new_id}")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert [w["name"] for w in client.get("/api/worlds").json()] == [ALPHA, BETA]


def test_delete_a_non_empty_world_is_409(config) -> None:
    client = _client(config)
    r = client.delete("/api/worlds/1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_empty"
    assert len(client.get("/api/worlds").json()) == 2


def test_delete_an_unknown_world_is_404(config) -> None:
    assert _client(config).delete("/api/worlds/99").status_code == 404


def test_a_move_refused_at_the_claim_does_not_turn_continue_into_a_move(
    config, monkeypatch,
) -> None:
    ran: list = []
    client = _client(config, move=_fake_move(ran), stages=[Fake("load", ran)])

    def refuse(*args, **kwargs):
        raise jobs.JobRefused("a job is already running for this book")

    with monkeypatch.context() as patched:
        patched.setattr(jobs, "claim_job", refuse)
        r = client.post("/api/books/bb1/move", json={"world": ALPHA})
    assert r.status_code == 409
    conn = _db(config)
    assert jobs.move_target(conn, "bb1") is None
    conn.close()
    ran.clear()
    assert client.post("/api/books/bb1/continue").status_code == 202
    assert _wait(config, "bb1").state == jobs.DONE
    # The ordinary stages, from where the row is parked: no remove/reassign,
    # so the book stays in its own world.
    assert ran == [("load", "bb1")]
    assert _world_of(config, "bb1") == BETA


def test_move_to_the_own_world_with_an_unfinished_ordinary_job_does_nothing(
    config,
) -> None:
    ran: list = []
    conn = _db(config)
    jobs.start_job(conn, "bb1", "load", 1)
    jobs.fail(conn, "bb1", "boom")
    before = jobs.get_job(conn, "bb1")
    conn.close()
    client = _client(config, move=_fake_move(ran))
    r = client.post("/api/books/bb1/move", json={"world": BETA})
    assert r.status_code == 200 and r.json() == {"started": False}
    assert ran == []
    assert _job(config, "bb1") == before
