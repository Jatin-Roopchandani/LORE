"""`lore process` through the job store: pause, resume, refuse, force.

Extraction and load are faked; no Gemini, no network.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lore.cli import app
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.graph.loader import LoadReport
from lore.inference.gemini import GeminiQuotaError
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import QuotaStop
from lore.orchestrator.run import RunReport
from tests.fixtures import seed_book

runner = CliRunner()
BOOK = "abcdef0123456789"
REPORT = LoadReport(book_id=BOOK, entities=1, alias_links=0, rel_edges=1,
                    chunks=1, mentions=1)


def _setup(tmp_path: Path, monkeypatch, toml: str = "") -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("HOME", str(tmp_path))
    if toml:
        (tmp_path / "lore.toml").write_text(toml)
    seed_book(BOOK)


def _job(tmp_path: Path):
    conn = open_db(LoreConfig.load().sqlite_path)
    try:
        return jobs.get_job(conn, BOOK)
    finally:
        conn.close()


def _park(tmp_path: Path, resumable_at: datetime, stage: str = "load") -> None:
    conn = open_db(LoreConfig.load().sqlite_path)
    try:
        jobs.start_job(conn, BOOK, stage, 1)
        jobs.stop_quota(conn, BOOK, "Gemini quota")
        with conn:
            conn.execute("UPDATE book_jobs SET resumable_at = ?",
                         (resumable_at.isoformat(),))
    finally:
        conn.close()


def test_a_load_quota_stop_pauses_the_job_and_exits_1(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    with (
        patch("lore.cli.run_extraction",
              return_value=RunReport(book_id=BOOK, units_total=1, units_completed=1)),
        patch("lore.cli._load_book_sync",
              side_effect=GeminiQuotaError("HTTP 429 every model")),
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 1, result.output
    assert "process paused" in result.output
    assert "allowance resets at" in result.output
    assert "--resume" in result.output
    job = _job(tmp_path)
    assert (job.state, job.stage) == (jobs.WAITING_QUOTA, "load")


def test_resume_before_the_reset_refuses_without_running_anything(
    tmp_path, monkeypatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _park(tmp_path, datetime.now(timezone.utc) + timedelta(hours=3))
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync") as load,
    ):
        result = runner.invoke(app, ["process", BOOK, "--resume"])
    assert result.exit_code == 1
    assert "allowance resets at" in result.output
    assert "(your local time)" in result.output
    assert not extract.called and not load.called
    assert _job(tmp_path).state == jobs.WAITING_QUOTA


def test_the_refusal_names_the_local_time(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    at = datetime.now(timezone.utc) + timedelta(hours=3)
    _park(tmp_path, at)
    result = runner.invoke(app, ["process", BOOK, "--resume"])
    assert f"{at.astimezone():%H:%M}" in result.output


def test_force_overrides_the_refusal(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    _park(tmp_path, datetime.now(timezone.utc) + timedelta(hours=3))
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync", return_value=REPORT) as load,
    ):
        result = runner.invoke(app, ["process", BOOK, "--resume", "--force"])
    assert result.exit_code == 0, result.output
    assert load.called
    assert not extract.called, "the job was parked past extraction"
    assert _job(tmp_path).state == jobs.DONE


def test_resume_after_the_reset_continues_at_the_saved_stage(
    tmp_path, monkeypatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _park(tmp_path, datetime.now(timezone.utc) - timedelta(minutes=1))
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync", return_value=REPORT),
    ):
        result = runner.invoke(app, ["process", BOOK, "--resume"])
    assert result.exit_code == 0, result.output
    assert not extract.called
    assert "1 relationships" in result.output


def test_a_gemini_extraction_stop_parks_the_job_at_extract(
    tmp_path, monkeypatch
) -> None:
    _setup(tmp_path, monkeypatch, '[lore]\nextraction_provider = "gemini"\n')
    stopped = RunReport(book_id=BOOK, units_total=3, units_completed=1,
                        stopped_reason="Gemini cannot extract right now: HTTP 429")
    class Stopped:
        name = "extract"
        report = stopped

        def total(self, ctx) -> int:
            return 0

        def run(self, ctx, job_ctl) -> None:
            raise QuotaStop(stopped.stopped_reason)

    with (
        patch("lore.cli.ExtractStage", lambda **kw: Stopped()),
        patch("lore.cli._load_book_sync") as load,
    ):
        result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code == 1
    assert not load.called
    assert f"lore process {BOOK} --resume" in result.output
    job = _job(tmp_path)
    assert (job.state, job.stage) == (jobs.WAITING_QUOTA, "extract")


def test_skip_extract_runs_every_other_stage(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    with (
        patch("lore.cli.run_extraction") as extract,
        patch("lore.cli._load_book_sync", return_value=REPORT) as load,
    ):
        result = runner.invoke(app, ["process", BOOK, "--skip-extract"])
    assert result.exit_code == 0, result.output
    assert load.called and not extract.called
    assert _job(tmp_path).state == jobs.DONE


def test_extract_stage_seam_accepts_a_replacement(tmp_path, monkeypatch) -> None:
    from lore.cli import default_stages

    class Mine:
        name = "extract"

    stages = default_stages(LoreConfig.load(), extract_stage=Mine())
    assert [s.name for s in stages] == [
        "chunk", "extract", "load", "disambiguate", "beats"]
    assert any(isinstance(s, Mine) for s in stages)
    skipped = default_stages(LoreConfig.load(), skip_extract=True, extract_stage=Mine())
    assert "extract" not in [s.name for s in skipped]


def test_an_unknown_book_is_refused_plainly(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["process", "nope"])
    assert result.exit_code == 1
    assert "unknown book" in result.output


def test_books_shows_each_books_job_state_and_stage(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)
    result = runner.invoke(app, ["books"])
    assert "job:" not in result.output, "no job yet, nothing to show"
    _park(tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    result = runner.invoke(app, ["books"])
    assert "waiting_quota" in result.output
    assert "stage load" in result.output


def test_default_stages_pick_the_extract_stage_by_provider(tmp_path, monkeypatch) -> None:
    from lore.cli import _RunExtractionStage, default_stages
    from lore.orchestrator.extract_stage import ExtractStage

    _setup(tmp_path, monkeypatch, '[lore]\nextraction_provider = "gemini"\n')
    gemini = default_stages(LoreConfig.load())
    assert isinstance(gemini[1], ExtractStage)
    (tmp_path / "lore.toml").write_text('[lore]\nextraction_provider = "ollama"\n')
    other = default_stages(LoreConfig.load())
    assert isinstance(other[1], _RunExtractionStage)
