from pathlib import Path

from lore.config import LoreConfig


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
