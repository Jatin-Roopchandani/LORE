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

BOOK = "abcdef0123456789"


def _config(tmp_path: Path) -> LoreConfig:
    config = LoreConfig.load()
    return config.model_copy(
        update={
            "data_dir": tmp_path / "data",
            "kaggle": config.kaggle.model_copy(update={"username": "realuser"}),
            "extraction": config.extraction.model_copy(update={"work_unit_size": 2}),
        }
    )


def _seed(conn, n: int) -> None:
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"text {i}")
        for i in range(n)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=chunks)


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
    unit_dir = config.extractions_dir / BOOK / f"{BOOK}-u0000"
    assert (unit_dir / "triplets.parquet").is_file()
    assert (unit_dir / "manifest.json").is_file()
    conn.close()


def test_failed_unit_is_marked_and_counted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # units u0000, u0001

    def factory(cfg):
        return FakeClient(cfg, fail_units={f"{BOOK}-u0001"})

    report = run_extraction(config, conn, BOOK, client_factory=factory)
    assert report.units_completed == 1
    assert report.units_failed == 1
    assert unit_state(conn, f"{BOOK}-u0000") == "completed"
    assert unit_state(conn, f"{BOOK}-u0001") == "failed"
    conn.close()


def test_resume_skips_completed_and_retries_failed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)
    run_extraction(
        config, conn, BOOK,
        client_factory=lambda cfg: FakeClient(cfg, fail_units={f"{BOOK}-u0001"}),
    )
    second: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        second.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert second[0].runs == [f"{BOOK}-u0001"]  # only the failed unit re-ran
    assert report.units_completed == 1
    assert unit_state(conn, f"{BOOK}-u0001") == "completed"
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
    assert rows[0][0] == f"{BOOK}-u0000"
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

    def factory(cfg):
        return FakeClient(cfg, fail_units={f"{BOOK}-u0000"})

    run_extraction(config, conn, BOOK, client_factory=factory)
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (f"{BOOK}-u0000",)
    ).fetchone()
    assert row[0] == "failed"
    assert row[1]  # non-empty error message
    conn.close()


def test_failed_unit_log_is_captured_for_diagnosis(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)  # 1 unit at size 2
    kernel_log = (
        "ImportError: cannot import name 'GuidedDecodingParams' "
        "from 'vllm.sampling_params'"
    )

    def factory(cfg):
        return FakeClient(
            cfg, fail_units={f"{BOOK}-u0000"}, log_content=kernel_log,
        )

    run_extraction(config, conn, BOOK, client_factory=factory)
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (f"{BOOK}-u0000",)
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
    run_extraction(
        config, conn, BOOK,
        client_factory=lambda cfg: FakeClient(cfg, fail_units={f"{BOOK}-u0000"}),
    )
    run_extraction(
        config, conn, BOOK, resume=True,
        client_factory=lambda cfg: FakeClient(cfg),
    )
    row = conn.execute(
        "SELECT state, error FROM jobs WHERE detail = ?", (f"{BOOK}-u0000",)
    ).fetchone()
    assert row[0] == "completed"
    assert not row[1]  # empty or NULL
    conn.close()


def test_quota_exhaustion_stops_cleanly_with_partial_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 4)  # 2 units at size 2
    record_dispatch(conn, "prior-run", est_hours=29.99, week=iso_week())

    report = run_extraction(config, conn, BOOK, client_factory=lambda cfg: FakeClient(cfg))

    assert report.stopped_reason is not None
    assert unit_state(conn, f"{BOOK}-u0000") != "completed"
    conn.close()


def test_resume_reruns_a_dispatched_unit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ensure_dirs()
    conn = open_db(config.sqlite_path)
    _seed(conn, 2)  # 1 unit at size 2
    set_unit_state(conn, BOOK, f"{BOOK}-u0000", "dispatched")

    clients: list[FakeClient] = []

    def factory(cfg):
        client = FakeClient(cfg)
        clients.append(client)
        return client

    report = run_extraction(config, conn, BOOK, resume=True, client_factory=factory)
    assert clients[0].runs == [f"{BOOK}-u0000"]
    assert report.units_completed == 1
    assert unit_state(conn, f"{BOOK}-u0000") == "completed"
    conn.close()
