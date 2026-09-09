from pathlib import Path

from lore.config import ExtractionConfig, LoreConfig
from lore.inference.gemini import DEFAULT_MODELS


def test_defaults_when_no_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no lore.toml here
    cfg = LoreConfig.load()
    assert cfg.data_dir == Path("data")
    assert cfg.extraction_provider == "kaggle"
    assert cfg.ollama.model == "qwen2.5:3b-instruct-q4_K_M"
    assert cfg.ollama.host == "http://localhost:11434"


def test_load_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "lore.toml"
    toml.write_text(
        '[lore]\ndata_dir = "elsewhere"\nextraction_provider = "ollama"\n'
        '[lore.ollama]\nmodel = "llama3.2:3b"\n'
        '[lore.kaggle]\nusername = "someuser"\n'
    )
    cfg = LoreConfig.load(toml)
    assert cfg.data_dir == Path("elsewhere")
    assert cfg.extraction_provider == "ollama"
    assert cfg.ollama.model == "llama3.2:3b"
    assert cfg.kaggle.username == "someuser"


def test_env_overrides_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "envdata"))
    cfg = LoreConfig.load()
    assert cfg.data_dir == tmp_path / "envdata"


def test_ensure_dirs_creates_layout(tmp_path: Path) -> None:
    cfg = LoreConfig.load()
    cfg = cfg.model_copy(update={"data_dir": tmp_path / "d"})
    cfg.ensure_dirs()
    for sub in ("kuzu", "lancedb", "inbox"):
        assert (tmp_path / "d" / sub).is_dir()


def test_extraction_work_unit_size_default() -> None:
    # The dataclass default, NOT whatever lore.toml currently sets. Reading
    # the working tree's config made this test fail the moment the committed
    # value moved 10 -> 40 — the same coupling that once broke nine tests in
    # test_orchestrator_run.py when `extraction_provider` was flipped for a
    # local run. A test that asserts a tunable's shipped value is a tripwire
    # on config edits, not a test of behaviour.
    assert ExtractionConfig().work_unit_size == 10


def test_disambiguation_is_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = LoreConfig.load()
    assert cfg.disambiguation.enabled is False
    # The cascade is ordered by MEASURED score and quota, not by version:
    # the older Lite scored 12/12 against the newer one's 11/12, and both
    # carry 500 requests/day against Flash's 20.
    assert cfg.disambiguation.models == list(DEFAULT_MODELS)
    assert cfg.disambiguation.models[0] == "gemini-3.1-flash-lite"
    assert cfg.disambiguation_dir == Path("data") / "disambiguation"


def test_disambiguation_can_be_enabled_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "lore.toml"
    toml.write_text(
        "[lore.disambiguation]\nenabled = true\n"
        'models = ["gemini-3.8-flash", "gemini-3.6-flash"]\n'
    )
    cfg = LoreConfig.load(toml)
    assert cfg.disambiguation.enabled is True
    assert cfg.disambiguation.models == ["gemini-3.8-flash", "gemini-3.6-flash"]


def test_an_api_key_in_the_config_file_is_refused(tmp_path: Path) -> None:
    # The key lives in the environment and nowhere else: never in a config
    # file, a log, a commit, or the answer cache. A silently-ignored key in
    # lore.toml would be a key committed to git.
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.disambiguation]\napi_key = "AIza-secret"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "api_key" in str(exc.value)
