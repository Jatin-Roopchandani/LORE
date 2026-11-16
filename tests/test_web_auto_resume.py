"""`lore ui` (local mode) resumes a paused book on its own once it is due;
the Library says so. Fake stages and a fake clock; no network."""
from datetime import timedelta

from fastapi.testclient import TestClient

from lore.orchestrator import jobs
from lore.web.app import create_app
from tests.test_web_books import NOW, FakeStage, _job, _park, _wait_done, config  # noqa: F401


def _app(config, stages, auto_resume=True, clock=lambda: NOW):
    return create_app(config, stages_factory=lambda cfg: stages, clock=clock,
                      auto_resume=auto_resume)


def _resume(app):
    """One pass of the sweep, from the FastAPI app inside the middleware."""
    while not hasattr(app, "state"):
        app = app.app
    return app.state.resume_due()


def test_a_due_book_is_resumed_and_finishes(config):
    _park(config, "b1", NOW - timedelta(minutes=1))
    ran = []
    app = _app(config, [FakeStage("extract", ran=ran)])
    assert _resume(app) == "b1"
    assert _wait_done(config).state == jobs.DONE
    assert ran == ["b1"]


def test_a_book_not_yet_due_waits(config):
    _park(config, "b1", NOW + timedelta(minutes=5))
    ran = []
    app = _app(config, [FakeStage("extract", ran=ran)])
    assert _resume(app) is None
    assert _job(config).state == jobs.WAITING_QUOTA and ran == []


def test_the_earliest_due_book_goes_first_one_at_a_time(config):
    _park(config, "b1", NOW - timedelta(minutes=1))
    _park(config, "b2", NOW - timedelta(minutes=9))
    app = _app(config, [FakeStage()])
    assert _resume(app) == "b2"
    _wait_done(config, "b2")
    assert _resume(app) == "b1"
    _wait_done(config, "b1")


def test_the_library_says_it_continues_by_itself(config):
    _park(config, "b1", NOW + timedelta(hours=3))
    on = TestClient(_app(config, []), base_url="http://127.0.0.1:8766")
    assert on.get("/api/books").json()["books"][0]["job"]["auto_resume"] is True
    off = TestClient(_app(config, [], auto_resume=False), base_url="http://127.0.0.1:8766")
    assert off.get("/api/books").json()["books"][0]["job"]["auto_resume"] is False


def test_a_book_waiting_for_continue_is_never_resumed(config):
    from lore.db.sqlite import open_db
    conn = open_db(config.sqlite_path)
    jobs.start_job(conn, "b1", "extract", 10, now=NOW)
    jobs.stop_switch(conn, "b1", "Paused: LORE couldn't reach Gemini after several"
                     " tries. Press Continue to try again.", now=NOW)
    conn.close()
    assert _resume(_app(config, [FakeStage()])) is None


def test_the_running_app_sweeps_by_itself(config, monkeypatch):
    monkeypatch.setattr("lore.web.app.RESUME_EVERY", 0.02)
    _park(config, "b1", NOW - timedelta(minutes=1))
    with TestClient(_app(config, [FakeStage("extract")]), base_url="http://127.0.0.1:8766"):
        assert _wait_done(config).state == jobs.DONE
