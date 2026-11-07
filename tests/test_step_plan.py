"""The processing step list (lore.orchestrator.plan): the stages the runner
would run, in its order, minus those that do no work; a time estimate per
step from the current stage's measured rate, the user's stored averages per
stage/provider/model, a documented default, or unknown. Offline."""
from datetime import timedelta
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs, plan
from lore.orchestrator.pipeline import run_book
from tests.fixtures import seed_book

BOOK = "abcdef0123456789"
from tests.test_worker import NOW  # noqa: E402


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book(BOOK)
    config = LoreConfig.load()
    conn = open_db(config.sqlite_path)
    yield config, conn
    conn.close()


def _chunks(conn):
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE book_id = ?", (BOOK,)).fetchone()[0]


def test_the_names_are_the_runners_own_stages_minus_those_that_do_no_work():
    from lore.cli import default_stages
    cfg = LoreConfig()
    names = plan.stage_names(default_stages(cfg, resume=True), cfg)
    assert names == ["extract", "load", "beats", "codex"]  # disambiguation off by default
    on = cfg.model_copy(update={
        "disambiguation": cfg.disambiguation.model_copy(update={"enabled": True}),
        "codex": cfg.codex.model_copy(update={"enabled": False})})
    assert plan.stage_names(default_stages(on, resume=True), on) == [
        "extract", "load", "disambiguate", "beats"]


def test_the_move_stages_have_their_own_list():
    from lore.cli import move_stages
    cfg = LoreConfig()
    assert plan.stage_names(move_stages(cfg, 2), cfg)[:3] == ["remove", "reassign", "load"]


class Stage:
    def __init__(self, name, items=4, clock=None, step=timedelta(seconds=10)):
        self.name, self._items, self._clock, self._step = name, items, clock, step

    def total(self, ctx):
        return self._items

    def run(self, ctx, job_ctl):
        for i in range(self._items + 1):
            if self._clock is not None and i:
                self._clock.now += self._step
            job_ctl.advance(i)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_a_completed_stage_stores_its_seconds_per_chunk(env):
    config, conn = env
    clock = Clock(NOW)
    run_book(conn, config, BOOK, [Stage("extract", 4, clock)], clock=clock)
    chunks = _chunks(conn)
    row = conn.execute("SELECT stage, provider, model, seconds, chunks FROM stage_timings"
                       ).fetchone()
    assert row[0] == "extract" and row[1] == "gemini" and row[2] == "gemini-3.1-flash-lite"
    assert row[3] == pytest.approx(40.0) and row[4] == pytest.approx(chunks)
    assert plan.seconds_per_chunk(conn, "extract", "gemini", "gemini-3.1-flash-lite") \
        == pytest.approx(40.0 / chunks)


def test_estimates_fall_back_from_model_to_provider_to_stage_to_default(env):
    _, conn = env
    with conn:
        conn.execute("INSERT INTO stage_timings (stage, provider, model, seconds, chunks)"
                     " VALUES ('load', 'anthropic', 'claude-haiku-5-5', 100, 50)")
    assert plan.seconds_per_chunk(conn, "load", "anthropic", "claude-haiku-5-5") == 2.0
    assert plan.seconds_per_chunk(conn, "load", "anthropic", "claude-opus-5-5") == 2.0
    assert plan.seconds_per_chunk(conn, "load", "gemini", None) == 2.0
    assert plan.seconds_per_chunk(conn, "extract", "gemini", None) == \
        plan.DEFAULT_SECONDS_PER_CHUNK["extract"]
    assert plan.seconds_per_chunk(conn, "codex", "gemini", None) is None  # unknown


def test_the_step_list_marks_done_current_and_upcoming_with_estimates(env):
    _, conn = env
    chunks = _chunks(conn)
    names = ["extract", "load", "beats", "codex"]
    jobs.claim_job(conn, BOOK, now=NOW)
    jobs.start_job(conn, BOOK, "load", 100, now=NOW, guard=False)
    jobs.reset_stage_clock(conn, BOOK)
    jobs.advance(conn, BOOK, 10, now=NOW)            # resumed at 10: the clock starts here
    jobs.advance(conn, BOOK, 30, now=NOW + timedelta(seconds=40))
    with conn:
        conn.execute("INSERT INTO stage_timings (stage, provider, model, seconds, chunks)"
                     " VALUES ('beats', 'gemini', 'm', ?, ?)", (chunks * 3.0, chunks))
    job = jobs.get_job(conn, BOOK)
    out = plan.steps(conn, BOOK, job, names, NOW + timedelta(seconds=40),
                     provider="gemini", model="m")
    states = [(s["stage"], s["state"]) for s in out["steps"]]
    assert states == [("extract", "done"), ("load", "current"), ("beats", "upcoming"),
                      ("codex", "upcoming")]
    assert (out["step"], out["of"]) == (2, 4)
    current = out["steps"][1]
    assert (current["done"], current["total"]) == (30, 100)
    assert current["seconds_left"] == pytest.approx(2.0 * 70)  # 40 s / 20 items, 70 left
    assert out["steps"][2]["seconds_left"] == pytest.approx(chunks * 3.0)
    assert out["steps"][3]["seconds_left"] is None   # nothing known: "a few minutes"
    assert out["unknown_left"] is True
    assert out["seconds_left"] == round(140 + chunks * 3.0)


def test_the_current_rate_waits_for_a_few_items(env):
    _, conn = env
    jobs.claim_job(conn, BOOK, now=NOW)
    jobs.start_job(conn, BOOK, "codex", 100, now=NOW, guard=False)
    jobs.reset_stage_clock(conn, BOOK)
    jobs.advance(conn, BOOK, 0, now=NOW)
    jobs.advance(conn, BOOK, 2, now=NOW + timedelta(seconds=5))
    out = plan.steps(conn, BOOK, jobs.get_job(conn, BOOK), ["codex"],
                     NOW + timedelta(seconds=5), provider="gemini", model=None)
    assert out["steps"][0]["seconds_left"] is None and out["seconds_left"] is None


def test_no_list_for_a_finished_book_or_none(env):
    _, conn = env
    assert plan.steps(conn, BOOK, None, ["extract"], NOW) is None
    jobs.start_job(conn, BOOK, "codex", 1, now=NOW)
    jobs.done(conn, BOOK, now=NOW)
    assert plan.steps(conn, BOOK, jobs.get_job(conn, BOOK), ["extract"], NOW) is None


def test_a_job_at_the_hidden_chunk_stage_shows_the_first_step_as_current(env):
    _, conn = env
    jobs.claim_job(conn, BOOK, now=NOW)  # stage 'chunk'
    out = plan.steps(conn, BOOK, jobs.get_job(conn, BOOK), ["extract", "load"], NOW,
                     provider="gemini")
    assert [s["state"] for s in out["steps"]] == ["current", "upcoming"]
    assert out["steps"][0]["seconds_left"] == pytest.approx(
        _chunks(conn) * plan.DEFAULT_SECONDS_PER_CHUNK["extract"])


def test_the_library_api_carries_the_plan(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lore.web.app import create_app
    from tests.auth_fixtures import BASE_URL

    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book(BOOK)
    cfg = LoreConfig.load()
    conn = open_db(cfg.sqlite_path)
    jobs.claim_job(conn, BOOK, now=NOW)
    jobs.start_job(conn, BOOK, "beats", 5, now=NOW, guard=False)
    conn.close()

    class S:
        def __init__(self, name):
            self.name = name

    app = create_app(cfg, stages_factory=lambda c: [S("extract"), S("load"), S("beats")])
    body = TestClient(app, base_url=BASE_URL).get("/api/books").json()
    steps = body["books"][0]["job"]["plan"]
    assert [s["stage"] for s in steps["steps"]] == ["extract", "load", "beats"]
    assert (steps["step"], steps["of"]) == (3, 3)
