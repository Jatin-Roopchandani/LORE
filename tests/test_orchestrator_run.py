import json
from pathlib import Path

import pytest

from lore.config import KAGGLE_REMOVED, LoreConfig
from lore.db.sqlite import open_db
from lore.extract.embed import write_unit_outputs
from lore.extract.runner import ChunkResult, ExtractionResult
from lore.extract.schemas import SCHEMA_VERSION, ChunkExtraction
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator import local
from lore.orchestrator.run import RunReport, run_extraction, set_unit_state, unit_state
from lore.orchestrator.workunit import pack_units, validate_output

BOOK = "abcdef0123456789"


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(n)
    ]


def _unit_ids(n: int, size: int = 2) -> list[str]:
    """Unit ids are content-addressed (FIX 5): compute the real ids
    pack_units would assign for a given seed size, rather than hardcoding
    the old `{book_id}-u{index:04d}` format."""
    return [unit.unit_id for unit in pack_units(BOOK, _chunks(n), size=size)]


def _config(tmp_path: Path) -> LoreConfig:
    config = LoreConfig.load()
    return config.model_copy(
        update={
            "data_dir": tmp_path / "data",
            # Pinned, not inherited from the working tree's lore.toml.
            "extraction_provider": "ollama",
            "extraction": config.extraction.model_copy(update={"work_unit_size": 2}),
        }
    )


def _seed(conn, n: int) -> None:
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=_chunks(n))


class FakeLocal:
    """Stands in for `local.run_unit_local`: writes the real artifacts of an
    empty extraction, or fails the units in `fail_units`; counts runs."""

    def __init__(self, fail_units: set[str] | None = None) -> None:
        self.fail_units = fail_units or set()
        self.runs: list[str] = []

    async def __call__(self, config, unit, dest, provider=None):
        self.runs.append(unit.unit_id)
        if unit.unit_id in self.fail_units:
            raise RuntimeError(f"the local model crashed on {unit.unit_id}")
        result = ExtractionResult(results=[
            ChunkResult(chunk.id, ChunkExtraction(entities=[], triplets=[]))
            for chunk in unit.chunks])
        write_unit_outputs(
            dest, unit_id=unit.unit_id, book_id=unit.book_id,
            chunk_ids=[chunk.id for chunk in unit.chunks], result=result,
            vectors=[[0.0] * 768 for _ in unit.chunks], model="qwen2.5:3b",
            embed_model="nomic-embed-text", gpu_seconds=1.0,
            schema_version=SCHEMA_VERSION, started_at="2026-11-08T00:00:00+00:00")
        return validate_output(unit, dest)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)

    def run(fake: FakeLocal, **kwargs) -> RunReport:
        monkeypatch.setattr(local, "run_unit_local", fake)
        return run_extraction(config, conn, BOOK, **kwargs)

    yield config, conn, run
    conn.close()


def test_runs_every_unit_and_reports(env) -> None:
    config, conn, run = env
    _seed(conn, 5)  # 3 units at size 2
    fake = FakeLocal()
    report = run(fake)
    assert isinstance(report, RunReport)
    assert report.units_total == 3
    assert report.units_completed == 3
    assert report.chunks_ok == 5
    assert fake.runs == _unit_ids(5)


def test_artifacts_land_under_the_book_directory(env) -> None:
    config, conn, run = env
    _seed(conn, 2)
    run(FakeLocal())
    unit_dir = config.extractions_dir / BOOK / _unit_ids(2)[0]
    assert (unit_dir / "triplets.parquet").is_file()
    assert (unit_dir / "manifest.json").is_file()


def test_failed_unit_is_marked_and_counted(env) -> None:
    config, conn, run = env
    _seed(conn, 4)  # units u0000, u0001
    u0000, u0001 = _unit_ids(4)
    report = run(FakeLocal(fail_units={u0001}))
    assert report.units_completed == 1
    assert report.units_failed == 1
    assert unit_state(conn, u0000) == "completed"
    assert unit_state(conn, u0001) == "failed"


def test_resume_skips_completed_and_retries_failed(env) -> None:
    config, conn, run = env
    _seed(conn, 4)
    _, u0001 = _unit_ids(4)
    run(FakeLocal(fail_units={u0001}))
    second = FakeLocal()
    report = run(second, resume=True)
    assert second.runs == [u0001]  # only the failed unit re-ran
    assert report.units_completed == 1
    assert unit_state(conn, u0001) == "completed"


def test_resume_is_a_no_op_when_everything_completed(env) -> None:
    config, conn, run = env
    _seed(conn, 2)
    run(FakeLocal())
    again = FakeLocal()
    report = run(again, resume=True)
    assert report.units_total == 1
    assert report.units_completed == 0  # nothing left to do
    assert again.runs == []


def test_unknown_book_raises(env) -> None:
    config, conn, _run = env
    with pytest.raises(ValueError, match="no chunks"):
        run_extraction(config, conn, "nosuchbook")


def test_failure_reason_is_persisted(env) -> None:
    config, conn, run = env
    _seed(conn, 2)
    u0000 = _unit_ids(2)[0]
    run(FakeLocal(fail_units={u0000}))
    state, error = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0000,)).fetchone()
    assert state == "failed"
    assert "crashed" in error


def test_successful_run_clears_a_previous_error(env) -> None:
    config, conn, run = env
    _seed(conn, 2)
    u0000 = _unit_ids(2)[0]
    run(FakeLocal(fail_units={u0000}))
    run(FakeLocal(), resume=True)
    state, error = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0000,)).fetchone()
    assert state == "completed"
    assert not error  # empty or NULL


def test_resume_reruns_a_dispatched_unit(env) -> None:
    config, conn, run = env
    _seed(conn, 2)  # 1 unit at size 2
    u0000 = _unit_ids(2)[0]
    set_unit_state(conn, BOOK, u0000, "dispatched")
    fake = FakeLocal()
    report = run(fake, resume=True)
    assert fake.runs == [u0000]
    assert report.units_completed == 1
    assert unit_state(conn, u0000) == "completed"


def test_the_removed_kaggle_tier_is_refused_before_touching_any_unit(env) -> None:
    config, conn, _run = env
    _seed(conn, 2)
    old = config.model_copy(update={"extraction_provider": "kaggle"})
    with pytest.raises(ValueError) as caught:
        run_extraction(old, conn, BOOK)
    assert str(caught.value) == KAGGLE_REMOVED
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- Task R1: extraction_provider = "gemini" --------------------------------

import httpx  # noqa: E402

from lore.inference.gemini import LITE_MODELS, GeminiClient  # noqa: E402
from lore.orchestrator.local import GeminiTier  # noqa: E402

GOOD_JSON = json.dumps({
    "entities": [{"name": "Holmes", "kind": "Character"}],
    "triplets": [{"subject": "Holmes", "predicate": "INTERACTS_WITH",
                  "object": "Watson", "evidence": "e", "confidence": 0.9}],
})


class _Embedder:
    closed = 0

    async def embed(self, texts, model):
        return [[0.0] * 768 for _ in texts]

    async def aclose(self):
        type(self).closed += 1


def _gemini_config(tmp_path: Path) -> LoreConfig:
    return _config(tmp_path).model_copy(update={"extraction_provider": "gemini"})


def _tier_factory(handler, made: list):
    async def no_sleep(_seconds: float) -> None:
        return None

    def factory(config):
        client = GeminiClient("AIza-not-a-real-key", models=list(LITE_MODELS),
                              transport=httpx.MockTransport(handler), sleep=no_sleep)
        tier = GeminiTier(config=config, client=client, embedder=_Embedder())
        made.append(tier)
        return tier

    return factory


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": GOOD_JSON}]}}]
    })


def test_gemini_units_complete_without_quota_rows(tmp_path: Path) -> None:
    config = _gemini_config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 5)  # 3 units at size 2
    made: list[GeminiTier] = []
    report = run_extraction(config, conn, BOOK,
                            gemini_tier_factory=_tier_factory(_ok_handler, made))
    assert (report.units_total, report.units_completed, report.units_failed) == (3, 3, 0)
    assert report.chunks_ok == 5
    assert all(unit_state(conn, uid) == "completed" for uid in _unit_ids(5))
    assert conn.execute("SELECT COUNT(*) FROM quota_ledger").fetchone()[0] == 0
    # ONE client for the whole run, so per-model throttling spans units; and
    # it is used across several units on one event loop and closed once.
    assert len(made) == 1
    assert made[0].client.requests_made == 5
    assert made[0].client._client.is_closed
    conn.close()


def test_gemini_quota_exhaustion_fails_the_unit_stops_and_dead_letters_nothing(
    tmp_path: Path,
) -> None:
    config = _gemini_config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # 2 units at size 2
    u0, u1 = _unit_ids(4)

    def exhausted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})

    made: list[GeminiTier] = []
    report = run_extraction(config, conn, BOOK,
                            gemini_tier_factory=_tier_factory(exhausted, made))
    assert report.units_completed == 0
    assert report.units_failed == 1
    assert report.chunks_dead == 0
    assert report.stopped_reason and "429" in report.stopped_reason
    state, error = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0,)).fetchone()
    assert state == "failed"
    assert "budget" in error and "--resume" in error
    # The run stopped rather than burning a request pair on every unit left.
    assert unit_state(conn, u1) == "queued"
    assert made[0].client.requests_made == len(LITE_MODELS)
    # Nothing dead-lettered, nothing on disk for the loader to trust.
    assert not list((config.extractions_dir / BOOK).rglob("dead_letter.jsonl"))

    # When the budget resets, --resume finishes the book.
    report = run_extraction(config, conn, BOOK, resume=True,
                            gemini_tier_factory=_tier_factory(_ok_handler, []))
    assert report.units_completed == 2 and report.chunks_dead == 0
    assert unit_state(conn, u0) == unit_state(conn, u1) == "completed"
    conn.close()


def test_gemini_without_a_key_refuses_before_touching_any_unit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.config/lore/gemini.env
    config = _gemini_config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    with pytest.raises(ValueError, match="no API key"):
        run_extraction(config, conn, BOOK)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()


def test_the_ollama_tier_is_still_dispatched_as_before(tmp_path: Path, monkeypatch) -> None:
    from lore.orchestrator import local

    config = _config(tmp_path).model_copy(update={"extraction_provider": "ollama"})
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    ran: list[str] = []

    async def fake_local(cfg, unit, dest, provider=None):
        ran.append(unit.unit_id)
        raise RuntimeError("ollama down")

    monkeypatch.setattr(local, "run_unit_local", fake_local)
    report = run_extraction(config, conn, BOOK)
    assert ran == _unit_ids(2)
    assert report.units_failed == 1 and report.stopped_reason is None
    conn.close()
