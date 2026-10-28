"""The books API: job status, Continue/Process, startup recovery.

Fake stages and a fake clock; no Gemini, no network.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.web.app import create_app
from tests.mcp_fixtures import build_store_triple

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


class FakeStage:
    def __init__(self, name="chunk", gate=None, ran=None):
        self.name, self._gate, self._ran = name, gate, ran

    def total(self, ctx):
        return 3

    def run(self, ctx, job_ctl):
        job_ctl.advance(1)
        if self._ran is not None:
            self._ran.append(ctx.book_id)
        if self._gate is not None:
            assert self._gate.wait(5)


@pytest.fixture
def config(tmp_path):
    return build_store_triple(
        tmp_path, books=[("b1", "A Study in Scarlet"), ("b2", "The Sign of Four")],
        chunks=[("c1", "b1", 1)])


def _client(config, stages):
    app = create_app(config, stages_factory=lambda cfg: stages, clock=lambda: NOW)
    return TestClient(app, base_url="http://127.0.0.1:8766")


def _job(config, book="b1"):
    conn = open_db(config.sqlite_path)
    try:
        return jobs.get_job(conn, book)
    finally:
        conn.close()


def _wait_done(config, book="b1"):
    for _ in range(200):
        job = _job(config, book)
        if job is not None and job.state in (jobs.DONE, jobs.FAILED):
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _park(config, book, resumable_at):
    conn = open_db(config.sqlite_path)
    jobs.start_job(conn, book, "extract", 10, done=4, now=NOW)
    jobs.stop_quota(conn, book, "allowance spent", now=NOW)
    with conn:
        conn.execute("UPDATE book_jobs SET resumable_at = ? WHERE book_id = ?",
                     (resumable_at.isoformat(), book))
    conn.close()


def test_get_books_shape_for_an_unprocessed_book(config):
    body = _client(config, []).get("/api/books").json()
    assert set(body) == {"books", "gemini"}
    first = body["books"][0]
    assert first == {"id": "b1", "title": "A Study in Scarlet", "author": None,
                     "status": "registered", "series": None, "series_index": None,
                     "job": None}
    g = body["gemini"]
    assert set(g) == {"used_today", "limit", "answer_reserve"}
    assert g["used_today"] == 0 and g["limit"] > 0 and g["answer_reserve"] == 60


def test_get_books_job_shape_while_paused(config):
    reset = NOW + timedelta(hours=5)
    _park(config, "b1", reset)
    job = _client(config, []).get("/api/books").json()["books"][0]["job"]
    assert job["stage"] == "extract" and job["state"] == "waiting_quota"
    assert (job["done"], job["total"]) == (4, 10)
    assert job["stopped_reason"].startswith("allowance spent")
    assert datetime.fromisoformat(job["resumable_at"]) == reset
    assert job["can_continue"] is False
    assert "resets at" in job["continue_reason"]


def test_get_books_can_continue_after_the_reset(config):
    _park(config, "b1", NOW - timedelta(minutes=1))
    job = _client(config, []).get("/api/books").json()["books"][0]["job"]
    assert job["can_continue"] is True and job["continue_reason"] == ""


def test_continue_starts_processing_an_unprocessed_book(config):
    ran = []
    client = _client(config, [FakeStage(ran=ran)])
    r = client.post("/api/books/b1/continue")
    assert r.status_code == 202 and r.json() == {"started": True}
    assert _wait_done(config).state == jobs.DONE
    assert ran == ["b1"]


def test_continue_resumes_a_parked_job_after_the_reset(config):
    _park(config, "b1", NOW - timedelta(minutes=1))
    client = _client(config, [FakeStage()])
    assert client.post("/api/books/b1/continue").status_code == 202
    assert _wait_done(config).state == jobs.DONE


def test_continue_before_the_reset_is_409_and_leaves_the_row(config):
    _park(config, "b1", NOW + timedelta(hours=3))
    client = _client(config, [FakeStage()])
    r = client.post("/api/books/b1/continue")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "not_resumable" and "resets at" in err["message"]
    assert _job(config).state == jobs.WAITING_QUOTA


def test_continue_while_a_job_runs_is_409_and_only_one_runs(config):
    gate = threading.Event()
    client = _client(config, [FakeStage(gate=gate)])
    assert client.post("/api/books/b1/continue").status_code == 202
    for other in ("b1", "b2"):
        r = client.post(f"/api/books/{other}/continue")
        assert r.status_code == 409, other
        assert r.json()["error"]["code"] == "busy"
    gate.set()
    assert _wait_done(config).state == jobs.DONE
    for _ in range(100):  # the slot frees once the thread returns
        if client.post("/api/books/b2/continue").status_code == 202:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("slot never freed")


def test_continue_unknown_book_is_404(config):
    r = _client(config, [FakeStage()]).post("/api/books/nope/continue")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_a_stage_failure_is_recorded_not_raised(config):
    class Boom(FakeStage):
        def run(self, ctx, job_ctl):
            raise RuntimeError("kaput")

    client = _client(config, [Boom()])
    assert client.post("/api/books/b1/continue").status_code == 202
    job = _wait_done(config)
    assert job.state == jobs.FAILED and "kaput" in job.stopped_reason


def test_startup_recovers_stale_running_jobs(config):
    conn = open_db(config.sqlite_path)
    jobs.start_job(conn, "b1", "extract", 10, now=NOW - timedelta(minutes=10))
    conn.close()
    _client(config, [])
    job = _job(config)
    assert job.state == jobs.IDLE and job.stopped_reason == "interrupted"


def test_continue_marks_running_before_the_202_returns(config):
    gate = threading.Event()
    client = _client(config, [FakeStage(gate=gate)])
    try:
        assert client.post("/api/books/b1/continue").status_code == 202
        job = _job(config)  # no waiting: the 202 implies running
        assert job is not None and job.state == jobs.RUNNING
        assert job.updated_at == NOW
    finally:
        gate.set()
    _wait_done(config)


def test_get_books_recovers_a_dead_runner(config):
    client = _client(config, [])
    conn = open_db(config.sqlite_path)
    jobs.start_job(conn, "b1", "extract", 10, now=NOW - timedelta(minutes=10))
    conn.close()
    job = client.get("/api/books").json()["books"][0]["job"]
    assert job["state"] == "idle" and job["can_continue"] is True


def test_a_running_job_stays_fresh_with_a_moving_clock(config):
    ticks = {"t": NOW}

    def clock():
        ticks["t"] += timedelta(seconds=40)
        return ticks["t"]

    gate = threading.Event()

    class Slow(FakeStage):
        def run(self, ctx, job_ctl):
            job_ctl.advance(1)
            job_ctl.advance(2)
            gate.wait(5)

    app = create_app(config, stages_factory=lambda cfg: [Slow()], clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    try:
        assert client.post("/api/books/b1/continue").status_code == 202
        for _ in range(200):
            j = _job(config)
            if j.done_items == 2:
                break
            time.sleep(0.02)
        assert _job(config).updated_at > NOW + timedelta(seconds=40), \
            "stamps follow the clock, not the start time"
    finally:
        gate.set()
    _wait_done(config)
