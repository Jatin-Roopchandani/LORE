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


# --- Gemini's free tier: the daily request limit ----------------------------------------

def _big_book(conn, book="big", chunks=1050, chars=3000, chapters=30):
    conn.execute("INSERT INTO books (id, title, file_path, status) VALUES (?, 'Big', 'x', 'chunked')",
                 (book,))
    for i in range(chunks):
        conn.execute("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, ?, ?, 0, ?, ?)", (f"{book}:{i}", book, i % chapters, i, "x" * chars))
    conn.commit()


def _tier(today_left=600, per_day=1000, reset=None):
    from datetime import datetime, timezone
    return plan.FreeTier(today_left=today_left, per_day=per_day,
                         reset=reset or datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc))


def test_requests_left_counts_extraction_and_the_later_stages_that_ask(env):
    _config, conn = env
    _big_book(conn)
    jobs.start_job(conn, "big", "extract", 1050, now=NOW)
    jobs.advance(conn, "big", 50, now=NOW)
    job = jobs.get_job(conn, "big")
    need = plan.requests_left(conn, "big", job, ["extract", "load", "beats", "codex"])
    assert need.extract_done == 50 and need.extract_left == 1000
    # beats: one request per 60,000 characters of a chapter (35 chunks x 3,000 = 105,000 -> 2 each)
    assert need.by_stage["beats"] == 60
    assert need.by_stage["codex"] == 150  # at most CODEX_MAX_PER_BOOK entities
    assert need.total == sum(need.by_stage.values())
    assert need.rough  # codex and the load's questions are guesses until they run


def test_a_book_that_fits_today_gets_no_free_tier_line(env):
    _config, conn = env
    _big_book(conn, chunks=40)
    jobs.start_job(conn, "big", "extract", 40, now=NOW)
    out = plan.steps(conn, "big", jobs.get_job(conn, "big"), ["extract", "load"], NOW,
                     provider="gemini", free_tier=_tier(today_left=600))
    assert "free_tier" not in out


def test_a_book_past_todays_limit_says_where_it_runs_out_and_when_it_finishes(env):
    _config, conn = env
    _big_book(conn)
    jobs.start_job(conn, "big", "extract", 1050, now=NOW)
    out = plan.steps(conn, "big", jobs.get_job(conn, "big"), ["extract", "load"], NOW,
                     provider="gemini", free_tier=_tier(today_left=600, per_day=1000))
    tier = out["free_tier"]
    assert tier["runs_out_at_part"] == 600 and tier["days"] == 1
    assert tier["requests_left"] > 1050 and tier["today_left"] == 600
    # the rest (~450 extraction requests plus the load's) after the reset, at ~4 s a request
    from datetime import datetime
    finish = datetime.fromisoformat(tier["finish_at"])
    assert datetime.fromisoformat("2026-11-06T08:00:00+00:00") < finish < datetime.fromisoformat(
        "2026-11-06T09:00:00+00:00")


def test_several_days_when_the_rest_needs_more_than_a_day(env):
    _config, conn = env
    _big_book(conn, chunks=3000)
    jobs.start_job(conn, "big", "extract", 3000, now=NOW)
    out = plan.steps(conn, "big", jobs.get_job(conn, "big"), ["extract"], NOW,
                     provider="gemini", free_tier=_tier(today_left=100, per_day=1000))
    assert out["free_tier"]["days"] == 3 and out["free_tier"]["runs_out_at_part"] == 100


def test_no_free_tier_line_without_a_free_tier(env):
    _config, conn = env
    _big_book(conn)
    jobs.start_job(conn, "big", "extract", 1050, now=NOW)
    out = plan.steps(conn, "big", jobs.get_job(conn, "big"), ["extract", "load"], NOW, provider="claude")
    assert "free_tier" not in out


def _library_with_big_book(tmp_path, monkeypatch, toml=""):
    from lore.web.app import create_app
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    if toml:
        (tmp_path / "lore.toml").write_text(toml)
    cfg = LoreConfig.load()
    conn = open_db(cfg.sqlite_path)
    _big_book(conn, chunks=3000)
    jobs.claim_job(conn, "big", now=NOW)
    jobs.start_job(conn, "big", "extract", 3000, now=NOW, guard=False)
    conn.close()

    class S:
        def __init__(self, name):
            self.name = name
    return create_app(cfg, stages_factory=lambda c: [S("extract"), S("load")])


def test_the_library_api_carries_the_free_tier_line_on_gemini_free(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.auth_fixtures import BASE_URL
    app = _library_with_big_book(tmp_path, monkeypatch, '[lore.llm]\nprovider = "gemini"\n')
    plan_out = TestClient(app, base_url=BASE_URL).get("/api/books").json()["books"][0]["job"]["plan"]
    assert plan_out["free_tier"]["days"] >= 1 and plan_out["free_tier"]["requests_left"] > 3000


def test_a_paid_gemini_plan_has_no_free_tier_line(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.auth_fixtures import BASE_URL
    app = _library_with_big_book(tmp_path, monkeypatch,
                                 '[lore.llm]\nprovider = "gemini"\ngemini_paid = true\n')
    plan_out = TestClient(app, base_url=BASE_URL).get("/api/books").json()["books"][0]["job"]["plan"]
    assert "free_tier" not in plan_out
