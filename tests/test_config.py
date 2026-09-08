from pathlib import Path

from lore.config import ExtractionConfig, LoreConfig


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
