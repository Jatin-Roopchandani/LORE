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
                       chunks_ok=3, chunks_dead=0)
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


def test_extract_echoes_and_fails_when_stopped_early(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    report = RunReport(
        book_id=BOOK, units_total=2, units_completed=1,
        chunks_ok=3, chunks_dead=0,
        stopped_reason="every configured model refused the request",
    )
    with patch("lore.cli.run_extraction", return_value=report):
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code != 0
    assert "every configured model refused the request" in result.output


def test_extract_fails_cleanly_for_unknown_book(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["extract", "nosuchbook"])
    assert result.exit_code == 1
    assert "no chunks" in result.output


# --- Task R1: extraction_provider = "gemini" needs a key up front ----------

def _no_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_extract_on_gemini_without_a_key_refuses_before_any_unit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _no_key(tmp_path, monkeypatch)
    (tmp_path / "lore.toml").write_text('[lore]\nextraction_provider = "gemini"\n')
    _seed(tmp_path)
    with patch("lore.cli.run_extraction") as run:
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 2, result.output
    assert not run.called
    assert 'extraction_provider = "gemini"' in result.output
    assert "no API key" in result.output and "gemini.env" in result.output


def test_extract_on_gemini_passes_the_resolved_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "k-from-env")
    (tmp_path / "lore.toml").write_text('[lore]\nextraction_provider = "gemini"\n')
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=1, chunks_ok=3)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["api_key"] == "k-from-env"
    assert "k-from-env" not in result.output
    assert "GPU-hours" not in result.output


def test_extract_on_ollama_the_default_needs_no_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _no_key(tmp_path, monkeypatch)
    _seed(tmp_path)
    report = RunReport(book_id=BOOK, units_total=1, units_completed=1)
    with patch("lore.cli.run_extraction", return_value=report) as run:
        result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["api_key"] is None


# --- the removed Kaggle tier --------------------------------------------------

OLD_KAGGLE_TOML = """[lore]
extraction_provider = "kaggle"

[lore.kaggle]
username = "someone"
kernel_slug = "lore-hello"
poll_timeout_s = 5400

[lore.extraction]
model = "Qwen/Qwen2.5-7B-Instruct-AWQ"
embed_model = "nomic-ai/nomic-embed-text-v1.5"
vllm_version = ""
work_unit_size = 10
"""


def test_an_old_kaggle_lore_toml_gets_a_clear_message_not_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _no_key(tmp_path, monkeypatch)
    (tmp_path / "lore.toml").write_text(OLD_KAGGLE_TOML)
    _seed(tmp_path)
    result = runner.invoke(app, ["extract", BOOK])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert 'extraction_provider = "kaggle" is no longer supported' in result.output
    assert '"gemini"' in result.output and '"ollama"' in result.output
    conn = open_db(tmp_path / "data" / "lore.db")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()


def test_an_old_kaggle_lore_toml_still_runs_every_other_command(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lore.toml").write_text(OLD_KAGGLE_TOML)
    _seed(tmp_path)
    result = runner.invoke(app, ["books"])
    assert result.exit_code == 0, result.output
    assert BOOK in result.output


def test_process_with_an_old_kaggle_lore_toml_fails_with_the_message(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _no_key(tmp_path, monkeypatch)
    (tmp_path / "lore.toml").write_text(OLD_KAGGLE_TOML)
    _seed(tmp_path)
    result = runner.invoke(app, ["process", BOOK])
    assert result.exit_code != 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert 'extraction_provider = "kaggle" is no longer supported' in result.output
