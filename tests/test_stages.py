"""Load, disambiguate, chunk and beats stages, driven through run_book.

Fakes only: no Gemini, no stores.
"""
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference.gemini import GeminiQuotaError
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.stages import (
    BeatsStage,
    ChunkStage,
    DisambiguateStage,
    LoadStage,
)
from lore.resolve.disambiguation import (
    MAX_BATCH,
    Candidate,
    DisambiguationStore,
    Question,
)
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


def test_chunk_stage_passes_a_chunked_book(env) -> None:
    config, conn = env
    job = run_book(conn, config, BOOK, [ChunkStage()])
    assert job.state == jobs.DONE


def test_chunk_stage_fails_a_book_with_no_chunks(env) -> None:
    config, conn = env
    with conn:
        conn.execute("DELETE FROM chunks")
    job = run_book(conn, config, BOOK, [ChunkStage()])
    assert job.state == jobs.FAILED
    assert "no chunks" in job.stopped_reason


def test_load_stage_keeps_the_report(env) -> None:
    config, conn = env
    stage = LoadStage(lambda cfg, c, book_id: ("report", book_id))
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE
    assert stage.report == ("report", BOOK)


def test_load_stage_turns_exhausted_models_into_a_quota_stop(env) -> None:
    config, conn = env

    def load(cfg, c, book_id):
        raise GeminiQuotaError("HTTP 429: every model refused")

    job = run_book(conn, config, BOOK, [LoadStage(load)])
    assert job.state == jobs.WAITING_QUOTA
    assert job.stage == "load"
    assert "429" in job.stopped_reason
    assert job.resumable_at is not None


def test_load_stage_other_errors_fail_the_job(env) -> None:
    config, conn = env

    def load(cfg, c, book_id):
        raise RuntimeError("boom")

    assert run_book(conn, config, BOOK, [LoadStage(load)]).state == jobs.FAILED


def test_load_stage_stops_at_the_answer_reserve(env) -> None:
    config, conn = env
    config = config.model_copy(update={
        "adjudication": config.adjudication.model_copy(update={"provider": "gemini"}),
        "processing": config.processing.model_copy(update={"answer_reserve": 10**9}),
    })
    calls = []
    job = run_book(conn, config, BOOK, [LoadStage(lambda *a: calls.append(a))])
    assert job.state == jobs.WAITING_QUOTA
    assert calls == [], "no load was started"


class _Client:
    """Answers each batch; the Nth call (1-based) in `fail_on` raises 429."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = 0
        self.closed = False

    async def generate(self, prompt: str) -> str:
        self.calls += 1
        if self.calls in self.fail_on:
            raise GeminiQuotaError("HTTP 429")
        # Refuse every question: "N. <name> -> none".
        lines = [f"{line.split('.')[0]}. {line.split('name: ')[1].split(' (')[0]} -> none"
                 for line in prompt.splitlines() if ". name: " in line]
        return "\n".join(lines)

    async def aclose(self) -> None:
        self.closed = True


def _questions(n: int) -> list[Question]:
    return [Question(mention=f"M{i:03d}", kind="Character", book_id=BOOK,
                     evidence=f"evidence {i}",
                     candidates=(Candidate(id=f"a{i}", name=f"A{i}"),
                                 Candidate(id=f"b{i}", name=f"B{i}")))
            for i in range(n)]


def _enable(config: LoreConfig) -> LoreConfig:
    return config.model_copy(update={
        "disambiguation": config.disambiguation.model_copy(update={"enabled": True})})


def _record(config: LoreConfig, n: int) -> None:
    store = DisambiguationStore(config.disambiguation_dir)
    for q in _questions(n):
        store.record(q)
    store.flush(BOOK)


def test_disambiguate_is_a_no_op_when_off(env) -> None:
    config, conn = env
    _record(config, 3)
    client = _Client()
    job = run_book(conn, config, BOOK, [DisambiguateStage(lambda cfg: client)])
    assert job.state == jobs.DONE
    assert client.calls == 0


def test_disambiguate_quota_stop_keeps_finished_batches_and_resumes(env) -> None:
    config, conn = env
    config = _enable(config)
    _record(config, MAX_BATCH + 5)  # two batches
    first = _Client(fail_on={2})
    job = run_book(conn, config, BOOK, [DisambiguateStage(lambda cfg: first)])
    assert job.state == jobs.WAITING_QUOTA, job.stopped_reason
    assert (job.done_items, job.total_items) == (1, 2)
    assert first.closed
    store = DisambiguationStore(config.disambiguation_dir)
    assert len(store.unanswered(store.pending())) == 5, "batch 1 stays cached"

    second = _Client()
    job = run_book(conn, config, BOOK, [DisambiguateStage(lambda cfg: second)],
                   force=True)
    assert job.state == jobs.DONE
    assert second.calls == 1, "only the unanswered batch is asked"
    store = DisambiguationStore(config.disambiguation_dir)
    assert store.unanswered(store.pending()) == []


def test_beats_stage_is_a_no_op(env) -> None:
    config, conn = env
    assert run_book(conn, config, BOOK, [BeatsStage()]).state == jobs.DONE

