from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from lore.cli import app
from lore.db.sqlite import open_db
from lore.ingest.models import Chunk
from lore.ingest.registry import ingest_book
from lore.orchestrator.run import RunReport

runner = CliRunner()
BOOK = "abcdef0123456789"


def _seed(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "data" / "lore.db")
    chunks = [
        Chunk(id=f"{BOOK}:1:{i}", book_id=BOOK, chapter_seq=1,
              scene_seq=0, seq=i, text=f"t{i}")
        for i in range(3)
    ]
    ingest_book(conn, book_id=BOOK, title="T", author=None,
                source="upload", file_path="/x/b.epub", chunks=chunks)
    conn.close()


def test_extract_reports_the_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=1,
                       chunks_ok=3, chunks_dead=0, gpu_hours=0.5)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 0, result.output
    assert "3 chunks" in result.output
    assert run.call_args.kwargs["resume"] is False


def test_extract_passes_resume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=0)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK, "--resume"])
    assert result.exit_code == 0
    assert run.call_args.kwargs["resume"] is True


def test_extract_fails_cleanly_for_unknown_book(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["extract", "nosuchbook"])
    assert result.exit_code == 1
    assert "no chunks" in result.output


def test_quota_reports_the_week(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0
    assert "remaining" in result.output.lower()
