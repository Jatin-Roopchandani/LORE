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


class _Tags:
    """Ollama's /api/tags reply, listing `names`."""

    def __init__(self, *names: str) -> None:
        self._names = names

    def json(self) -> dict:
        return {"models": [{"name": n} for n in self._names]}


def _doctor(*pulled: str):
    with patch("lore.cli.OllamaProvider") as ollama_cls, \
            patch("httpx.get", return_value=_Tags(*pulled)):
        ollama_cls.return_value.healthy = AsyncMock(return_value=True)
        ollama_cls.return_value.aclose = AsyncMock()
        return runner.invoke(app, ["doctor"])


def test_doctor_all_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # defaults; no lore.toml
    result = _doctor("nomic-embed-text:latest", "qwen2.5:3b-instruct-q4_K_M")
    assert result.exit_code == 0, result.output
    for check in ("config", "sqlite", "kuzu", "lancedb", "ollama", "embeddings", "model"):
        assert f"[ ok ] {check}" in result.output
    assert "kaggle" not in result.output.lower()


def test_doctor_fails_when_ollama_down(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    with patch("lore.cli.OllamaProvider") as ollama_cls:
        ollama_cls.return_value.healthy = AsyncMock(return_value=False)
        ollama_cls.return_value.aclose = AsyncMock()
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[fail] ollama" in result.output


def test_doctor_says_how_to_pull_a_missing_embedding_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = _doctor("qwen2.5:3b-instruct-q4_K_M")
    assert result.exit_code == 1
    assert "[fail] embeddings" in result.output and "ollama pull nomic-embed-text" in result.output


def _remote_toml(tmp_path: Path, llm: str = "") -> None:
    (tmp_path / "lore.toml").write_text(f'[lore]\nextraction_provider = "gemini"\n{llm}')


def test_doctor_fails_with_no_ai_model_and_says_what_to_set(tmp_path: Path, monkeypatch) -> None:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no key files
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _remote_toml(tmp_path)
    result = _doctor("nomic-embed-text:latest")
    assert result.exit_code == 1
    assert "[fail] model" in result.output
    assert "GEMINI_API_KEY" in result.output and "local model" in result.output


def test_doctor_names_the_model_with_a_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "x" * 39)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _remote_toml(tmp_path)
    result = _doctor("nomic-embed-text:latest")
    assert result.exit_code == 0, result.output
    assert "[ ok ] model — gemini" in result.output


def test_doctor_names_a_local_model_without_any_key(tmp_path: Path, monkeypatch) -> None:
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    _remote_toml(tmp_path, '[lore.llm]\nprovider = "openai_compatible"\n'
                 'base_url = "http://localhost:8080/v1"\nmodel = "qwen-28b"\n')
    result = _doctor("nomic-embed-text:latest")
    assert result.exit_code == 0, result.output
    assert "[ ok ] model — local model qwen-28b at http://localhost:8080/v1" in result.output


def test_quota_shows_the_gemini_section(tmp_path: Path, monkeypatch) -> None:
    config = _config_in(tmp_path, monkeypatch)
    conn = open_db(config.sqlite_path)
    record_call(conn, "gemini-3.5-flash-lite", today())
    conn.close()

    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "gemini-3.5-flash-lite" in result.output
    assert "1" in result.output


def test_quota_no_longer_reports_gpu_hours(tmp_path: Path, monkeypatch) -> None:
    _config_in(tmp_path, monkeypatch)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "h used" not in result.output and "GPU" not in result.output


def test_quota_without_gemini_calls_says_so(tmp_path: Path, monkeypatch) -> None:
    _config_in(tmp_path, monkeypatch)
    result = runner.invoke(app, ["quota"])
    assert result.exit_code == 0, result.output
    assert "no Gemini requests" in result.output
