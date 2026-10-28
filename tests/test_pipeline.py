from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs, pipeline
from lore.orchestrator.pipeline import JobRefused, QuotaStop, run_book
from lore.orchestrator.gemini_budget import record_call

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 29, 7, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "lore.db")
    c.execute("INSERT INTO books (id, title, file_path) VALUES ('b1','B','/x')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def config(tmp_path: Path) -> LoreConfig:
    return LoreConfig(data_dir=tmp_path)


class FakeStage:
    """Items are ints; `quota_after` lets that many requests succeed per run
    (across the whole test via the shared `requests` log), then stops."""

    def __init__(self, name, items, requests, quota_at=None, fail_at=None, check=False):
        self.name, self.items, self.requests = name, items, requests
        self.quota_at, self.fail_at, self.check = quota_at, fail_at, check
        self.done: set[int] = set()
        self.runs = 0

    def total(self, ctx):
        return len(self.items)

    def run(self, ctx, job_ctl):
        self.runs += 1
        job_ctl.advance(len(self.done))
        for i in self.items:
            if i in self.done:
                continue
            if self.check:
                job_ctl.spend_check()
            if self.quota_at == i:
                self.quota_at = None
                raise QuotaStop("limit hit")
            if self.fail_at == i:
                raise RuntimeError("kaboom")
            self.requests.append((self.name, i))
            self.done.add(i)
            job_ctl.advance(len(self.done))


def test_full_run_marks_done(conn, config) -> None:
    reqs = []
    stages = [FakeStage(n, [1, 2], reqs) for n in ("chunk", "extract", "ready")]
    job = run_book(conn, config, "b1", stages, now=NOW)
    assert (job.state, job.stage) == ("done", "ready")
    assert len(reqs) == 6


def test_quota_stop_then_resume_runs_each_item_once(conn, config) -> None:
    reqs = []
    chunk = FakeStage("chunk", [1], reqs)
    ext = FakeStage("extract", [1, 2, 3, 4], reqs, quota_at=3)
    load = FakeStage("load", [1, 2], reqs)
    stages = [chunk, ext, load]
    job = run_book(conn, config, "b1", stages, now=NOW)
    assert job.state == "waiting_quota" and job.stage == "extract"
    assert job.resumable_at == RESET
    assert (job.done_items, job.total_items) == (2, 4)
    assert "limit hit" in job.stopped_reason and "extract" in job.stopped_reason
    assert "2/4" in job.stopped_reason
    assert load.runs == 0

    # before the reset: refused, nothing runs
    with pytest.raises(JobRefused) as e:
        run_book(conn, config, "b1", stages, now=NOW + timedelta(hours=1))
    assert "resets" in str(e.value)
    assert chunk.runs == 1

    # after the reset: only missing items, finished stages not re-entered
    job = run_book(conn, config, "b1", stages, now=RESET)
    assert job.state == "done"
    assert chunk.runs == 1
    assert sorted(reqs) == sorted(
        [("chunk", 1)] + [("extract", i) for i in (1, 2, 3, 4)] + [("load", 1), ("load", 2)])


def test_force_overrides_refusal(conn, config) -> None:
    reqs = []
    ext = FakeStage("extract", [1, 2], reqs, quota_at=2)
    run_book(conn, config, "b1", [ext], now=NOW)
    job = run_book(conn, config, "b1", [ext], now=NOW + timedelta(minutes=5), force=True)
    assert job.state == "done"
    assert reqs == [("extract", 1), ("extract", 2)]


def test_other_exception_fails_job_and_retry_resumes(conn, config) -> None:
    reqs = []
    ext = FakeStage("extract", [1, 2, 3], reqs, fail_at=2)
    job = run_book(conn, config, "b1", [ext], now=NOW)
    assert job.state == "failed" and "kaboom" in job.stopped_reason
    assert job.resumable_at is None
    ext.fail_at = None
    job = run_book(conn, config, "b1", [ext], now=NOW + timedelta(seconds=1))
    assert job.state == "done"
    assert reqs == [("extract", 1), ("extract", 2), ("extract", 3)]


def test_fresh_running_row_blocks_second_runner(conn, config) -> None:
    jobs.start_job(conn, "b1", "extract", 3, now=NOW)
    with pytest.raises(JobRefused) as e:
        run_book(conn, config, "b1", [FakeStage("extract", [1], [])],
                 now=NOW + timedelta(seconds=30))
    assert "running" in str(e.value)
    # even with force: a live runner is not overridden
    with pytest.raises(JobRefused):
        run_book(conn, config, "b1", [FakeStage("extract", [1], [])],
                 now=NOW + timedelta(seconds=30), force=True)


def test_stale_running_row_is_taken_over(conn, config) -> None:
    jobs.start_job(conn, "b1", "extract", 3, now=NOW)
    reqs = []
    job = run_book(conn, config, "b1", [FakeStage("extract", [1], reqs)],
                   now=NOW + timedelta(minutes=10))
    assert job.state == "done" and reqs == [("extract", 1)]


def test_processing_budget_left(conn, config) -> None:
    from lore.inference.gemini import limits_for
    models = config.extraction.gemini_models
    total = sum(limits_for(m).rpd for m in dict.fromkeys(models))
    day = "2026-10-28"
    assert pipeline.processing_budget_left(conn, config, day) == total
    for _ in range(7):
        record_call(conn, models[0], day)
    record_call(conn, models[0], "2026-10-27")  # another day: ignored
    assert pipeline.processing_budget_left(conn, config, day) == total - 7


def test_reserve_stop(conn, config) -> None:
    from lore.inference.gemini import limits_for
    config = config.model_copy(update={"processing": config.processing.model_copy(
        update={"answer_reserve": 5})})
    models = list(dict.fromkeys(config.extraction.gemini_models))
    day = "2026-10-28"
    total = sum(limits_for(m).rpd for m in models)
    # leave exactly 7 requests: two items fit (7 -> 5 is not > reserve after #2)
    used = total - 7
    for m in models:
        n = min(used, limits_for(m).rpd)
        used -= n
        for _ in range(n):
            record_call(conn, m, day)
    reqs = []
    class Spender(FakeStage):
        def run(self, ctx, job_ctl):
            for i in self.items:
                job_ctl.spend_check()
                record_call(ctx.conn, models[-1], day)
                self.requests.append(i)
                job_ctl.advance(len(self.requests))

    sp = Spender("extract", [1, 2, 3, 4], reqs)
    job = run_book(conn, config, "b1", [sp], now=NOW)
    assert job.state == "waiting_quota"
    assert reqs == [1, 2]  # at 5 left (== reserve) it stops
    assert "reserve" in job.stopped_reason
    assert job.resumable_at == RESET
    assert pipeline.processing_budget_left(conn, config, day) == 5
