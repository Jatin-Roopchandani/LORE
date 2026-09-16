import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.quota import iso_week, record_dispatch
from lore.orchestrator.run import RunReport, run_extraction, set_unit_state, unit_state
from lore.orchestrator.workunit import pack_units

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
            # Pinned, not inherited: `LoreConfig.load()` reads the working
            # tree's lore.toml, and every test in this file exercises the
            # Kaggle dispatch path. Setting `extraction_provider = "ollama"`
            # there — a documented, supported knob — otherwise rerouted
            # `run_extraction` to the local path, wrote no quota rows, and
            # failed nine unrelated tests on an empty quota_ledger.
            "extraction_provider": "kaggle",
            "kaggle": config.kaggle.model_copy(update={"username": "realuser"}),
            "extraction": config.extraction.model_copy(update={"work_unit_size": 2}),
        }
    )


def _seed(conn, n: int) -> None:
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=_chunks(n))


class FakeClient:
    """Stands in for KaggleClient: writes plausible outputs, counts runs."""

    def __init__(
        self, config, *, fail_units: set[str] | None = None,
        log_content: str | None = None,
    ) -> None:
        self.fail_units = fail_units or set()
        self.log_content = log_content
        self.runs: list[str] = []
        self.pulled: list[Path] = []
        self._unit_ids: list[str] = []
        self._last_log = ""

    def push_dataset(self, dir: Path) -> None:
        manifest = json.loads((dir / "unit-manifest.json").read_text())
        self._unit_ids.append(manifest["unit_id"])
        self._chunk_ids = [
            json.loads(line)["chunk_id"]
            for line in (dir / "unit.jsonl").read_text().splitlines()
            if line.strip()
        ]

    def wait_dataset_ready(self, timeout_s: int = 300) -> None:
        return None

    def pull_output(self, dest: Path) -> None:
        """Mimics `kaggle kernels output`: best-effort re-fetch of whatever
        the last run produced, including its log."""
        self.pulled.append(dest)
        if self._last_log:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "kernel.log").write_text(self._last_log)

    def run_and_wait(self, kernel_dir: Path, dest: Path, on_tick=None) -> Path:
        unit_id = self._unit_ids[-1]
        self.runs.append(unit_id)
        dest.mkdir(parents=True, exist_ok=True)
        if unit_id in self.fail_units:
            if self.log_content is not None:
                (dest / "kernel.log").write_text(self.log_content)
                self._last_log = self.log_content
            return dest  # no artifacts -> validation fails
        ids = self._chunk_ids
        pq.write_table(
            pa.table({"chunk_id": ids, "subject": ["s"] * len(ids)}),
            dest / "triplets.parquet",
        )
        pq.write_table(
            pa.table({"chunk_id": ids, "vector": [[0.0] * 768] * len(ids)}),
            dest / "embeddings.parquet",
        )
        (dest / "dead_letter.jsonl").write_text("")
        (dest / "manifest.json").write_text(json.dumps({
            "unit_id": unit_id, "chunks_in": len(ids), "chunks_ok": len(ids),
            "chunks_dead": 0, "gpu_seconds": 3600.0,
            "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
        }))
        return dest


def test_runs_every_unit_and_reports(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 5)  # 3 units at size 2
    clients: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        clients.append(client)
        return client

    report = run_extraction(config, conn, BOOK, client_factory=factory)
    assert isinstance(report, RunReport)
    assert report.units_total == 3
    assert report.units_completed == 3
    assert report.chunks_ok == 5
    assert report.gpu_hours == pytest.approx(3.0)
    conn.close()


def test_artifacts_land_under_the_book_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    unit_dir = config.extractions_dir / BOOK / _unit_ids(2)[0]
    assert (unit_dir / "triplets.parquet").is_file()
    assert (unit_dir / "manifest.json").is_file()
    conn.close()


def test_failed_unit_is_marked_and_counted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # units u0000, u0001
    u0000, u0001 = _unit_ids(4)

    def factory(cfg):
        return FakeClient(cfg, fail_units={u0001})

    report = run_extraction(config, conn, BOOK, client_factory=factory)
    assert report.units_completed == 1
    assert report.units_failed == 1
    assert unit_state(conn, u0000) == "completed"
    assert unit_state(conn, u0001) == "failed"
    conn.close()


def test_resume_skips_completed_and_retries_failed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)
    _, u0001 = _unit_ids(4)
    run_extraction(
        config, conn, BOOK,
        client_factory=lambda cfg: FakeClient(cfg, fail_units={u0001}),
    )
    second: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        second.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert second[0].runs == [u0001]  # only the failed unit re-ran
    assert report.units_completed == 1
    assert unit_state(conn, u0001) == "completed"
    conn.close()


def test_resume_is_a_no_op_when_everything_completed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    seen: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        seen.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert report.units_total == 1
    assert report.units_completed == 0  # nothing left to do
    assert seen == [] or seen[0].runs == []
    conn.close()


def test_quota_is_recorded_per_unit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    rows = conn.execute(
        "SELECT run_ref, gpu_hours_actual FROM quota_ledger"
    ).fetchall()
    assert rows[0][0] == _unit_ids(2)[0]
    assert rows[0][1] == pytest.approx(1.0)  # 3600 gpu_seconds
    conn.close()


def test_unknown_book_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    with pytest.raises(ValueError, match="no chunks"):
        run_extraction(config, conn, "nosuchbook", client_factory=lambda cfg: FakeClient(cfg))
    conn.close()


def test_failure_reason_is_persisted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    u0000 = _unit_ids(2)[0]

    def factory(cfg):
        return FakeClient(cfg, fail_units={u0000})

    run_extraction(config, conn, BOOK, client_factory=factory)
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0000,)
    ).fetchone()
    assert row[0] == "failed"
    assert row[1]  # non-empty error message
    conn.close()


def test_failed_unit_log_is_captured_for_diagnosis(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)  # 1 unit at size 2
    u0000 = _unit_ids(2)[0]
    kernel_log = (
        "ImportError: cannot import name 'GuidedDecodingParams' "
        "from 'vllm.sampling_params'"
    )

    def factory(cfg):
        return FakeClient(
            cfg, fail_units={u0000}, log_content=kernel_log,
        )

    run_extraction(config, conn, BOOK, client_factory=factory)
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0000,)
    ).fetchone()
    assert row[0] == "failed"
    error = row[1]
    assert "missing artifacts" in error  # the original validation failure
    assert "GuidedDecodingParams" in error  # log content survived the fetch
    conn.close()


def test_successful_run_clears_a_previous_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)
    u0000 = _unit_ids(2)[0]
    run_extraction(
        config, conn, BOOK,
        client_factory=lambda cfg: FakeClient(cfg, fail_units={u0000}),
    )
    run_extraction(
        config, conn, BOOK, resume=True,
        client_factory=lambda cfg: FakeClient(cfg),
    )
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (u0000,)
    ).fetchone()
    assert row[0] == "completed"
    assert not row[1]  # empty or NULL
    conn.close()


def test_quota_exhaustion_stops_cleanly_with_partial_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # 2 units at size 2
    u0000 = _unit_ids(4)[0]
    record_dispatch(conn, "prior-run", est_hours=29.99, week=iso_week())

    report = run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))

    assert report.stopped_reason is not None
    assert unit_state(conn, u0000) != "completed"
    conn.close()


def test_resume_reruns_a_dispatched_unit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)  # 1 unit at size 2
    u0000 = _unit_ids(2)[0]
    set_unit_state(conn, BOOK, u0000, "dispatched")

    clients: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        clients.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert clients[0].runs == [u0000]
    assert report.units_completed == 1
    assert unit_state(conn, u0000) == "completed"
    conn.close()


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


def _no_kaggle(cfg):  # pragma: no cover - must never be called
    raise AssertionError("the gemini tier must not construct a Kaggle client")


def test_gemini_units_complete_without_kaggle_or_quota_rows(tmp_path: Path) -> None:
    config = _gemini_config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 5)  # 3 units at size 2
    made: list[GeminiTier] = []
    report = run_extraction(config, conn, BOOK, client_factory=_no_kaggle,
                            gemini_tier_factory=_tier_factory(_ok_handler, made))
    assert (report.units_total, report.units_completed, report.units_failed) == (3, 3, 0)
    assert report.chunks_ok == 5 and report.gpu_hours == 0.0
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
    report = run_extraction(config, conn, BOOK, client_factory=_no_kaggle,
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
    report = run_extraction(config, conn, BOOK, resume=True, client_factory=_no_kaggle,
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
        run_extraction(config, conn, BOOK, client_factory=_no_kaggle)
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
    report = run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))
    assert ran == _unit_ids(2)
    assert report.units_failed == 1 and report.stopped_reason is None
    conn.close()
