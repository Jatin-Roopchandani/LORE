from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from lore.cli import app

runner = CliRunner()


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
