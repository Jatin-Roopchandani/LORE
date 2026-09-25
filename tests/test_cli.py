from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lore.cli import app
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator.gemini_budget import record_call, today

runner = CliRunner()


def _config_in(tmp_path: Path, monkeypatch) -> LoreConfig:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # defaults; no lore.toml
    config = LoreConfig.load()
    config.ensure_dirs()
    return config


def test_doctor_all_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # defaults; no lore.toml
    with (
        patch("lore.cli.OllamaProvider") as ollama_cls,
        patch("lore.cli.KaggleClient") as kaggle_cls,
    ):
        ollama_cls.return_value.healthy = AsyncMock(return_value=True)
        ollama_cls.return_value.aclose = AsyncMock()
        kaggle_cls.return_value.authenticated.return_value = True
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for check in ("config", "sqlite", "kuzu", "lancedb", "ollama", "kaggle-auth"):
        assert f"[ ok ] {check}" in result.output


def test_doctor_fails_when_ollama_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with (
        patch("lore.cli.OllamaProvider") as ollama_cls,
        patch("lore.cli.KaggleClient") as kaggle_cls,
    ):
        ollama_cls.return_value.healthy = AsyncMock(return_value=False)
        ollama_cls.return_value.aclose = AsyncMock()
        kaggle_cls.return_value.authenticated.return_value = True
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[fail] ollama" in result.output


def test_quota_shows_the_gemini_section(tmp_path: Path, monkeypatch) -> None:
    config = _config_in(tmp_path, monkeypatch)
    conn = open_db(config.sqlite_path)
    record_call(conn, "gemini-3.5-flash-lite", today())
    conn.close()

    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "gemini-3.5-flash-lite" in result.output
    assert "1" in result.output


def test_quota_without_gemini_calls_says_so(tmp_path: Path, monkeypatch) -> None:
    _config_in(tmp_path, monkeypatch)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "no Gemini requests" in result.output
