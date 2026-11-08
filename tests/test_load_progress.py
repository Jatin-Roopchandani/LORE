"""The load stage ("Building the graph") reports real progress: the resolver
reports one step per mention (lore.progress), and the stage writes both counts
to the book's job row, throttled, ending at N / N. No stores, no network."""
import asyncio
from pathlib import Path

import pytest

from lore import progress
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs, stages
from lore.orchestrator.pipeline import JobCtl, StageContext, run_book
from lore.orchestrator.stages import LoadStage
from lore.resolve.resolver import Mention, resolve_mentions
from tests.fixtures import seed_book

BOOK = "abcdef0123456789"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    seed_book(BOOK)
    config = LoreConfig.load()
    conn = open_db(config.sqlite_path)
    yield config, conn
    conn.close()


def test_the_resolver_reports_one_step_per_mention():
    seen = []

    async def embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def never(canonical, mention):
        return False

    mentions = [Mention(f"Person {i}", "Character", "b1", "ctx") for i in range(4)]
    with progress.bound(lambda done, total: seen.append((done, total))):
        asyncio.run(resolve_mentions(mentions, embed, never))
    assert seen == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]


def test_nothing_bound_is_a_no_op():
    progress.report(1, 2)  # no listener: nothing happens


def test_the_load_stage_writes_real_counts(env, monkeypatch):
    config, conn = env
    monkeypatch.setattr(stages, "PROGRESS_EVERY", 0.0)
    rows = []
    real = JobCtl.progress

    def spy(self, done, total):
        real(self, done, total)
        row = jobs.get_job(self._ctx.conn, self._ctx.book_id)
        rows.append((row.stage, row.done_items, row.total_items))
    monkeypatch.setattr(JobCtl, "progress", spy)

    def load(cfg, c, book_id):
        for i in range(2900):
            progress.report(i, 2900)
        progress.report(2900, 2900)
        return "report"

    job = run_book(conn, config, BOOK, [LoadStage(load)])
    assert job.state == jobs.DONE
    assert rows[0] == ("load", 0, 2900) and rows[340] == ("load", 340, 2900)
    assert rows[-1] == ("load", 2900, 2900)


def test_progress_writes_are_throttled_but_the_last_one_lands(env, monkeypatch):
    config, conn = env
    monkeypatch.setattr(stages, "PROGRESS_EVERY", 3600.0)
    writes = []
    real = JobCtl.progress
    monkeypatch.setattr(JobCtl, "progress",
                        lambda self, d, t: (writes.append((d, t)), real(self, d, t)))
    seen_done = []

    def load(cfg, c, book_id):
        for i in range(1000):
            progress.report(i, 1000)
        progress.report(1000, 1000)
        seen_done.append(jobs.get_job(c, book_id))
        return None

    run_book(conn, config, BOOK, [LoadStage(load)])
    assert writes == [(0, 1000), (1000, 1000)]
    assert (seen_done[0].done_items, seen_done[0].total_items) == (1000, 1000)


def test_a_load_that_reports_nothing_still_ends_at_one_of_one(env):
    config, conn = env
    job = run_book(conn, config, BOOK, [LoadStage(lambda cfg, c, b: None)])
    assert job.state == jobs.DONE


def test_the_disambiguation_reload_does_not_touch_its_counts(env):
    config, conn = env
    stage = LoadStage(lambda cfg, c, b: progress.report(5, 10))
    jobs.start_job(conn, BOOK, "disambiguate", 3, 2)
    stage.reload(StageContext(conn, config, BOOK))  # no listener bound here
    row = jobs.get_job(conn, BOOK)
    assert (row.done_items, row.total_items) == (2, 3)
