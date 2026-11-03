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


def test_synthesis_is_local_by_default(tmp_path: Path, monkeypatch) -> None:
    # Opt-in: answers leave the machine only when the user says so.
    monkeypatch.chdir(tmp_path)
    cfg = LoreConfig.load()
    assert cfg.synthesis.provider == "ollama"
    # Lite only: 500 requests/day each. Flash gets 20 and is shared with
    # the disambiguator.
    assert cfg.synthesis.models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


def test_synthesis_can_opt_into_gemini_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.synthesis]\nprovider = "gemini"\nmodels = ["gemini-3.5-flash-lite"]\n')
    cfg = LoreConfig.load(toml)
    assert cfg.synthesis.provider == "gemini"
    assert cfg.synthesis.models == ["gemini-3.5-flash-lite"]


def test_synthesis_rejects_an_unknown_provider(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.synthesis]\nprovider = "openai"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "provider" in str(exc.value)


def test_an_api_key_in_the_synthesis_section_is_refused(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.synthesis]\nprovider = "gemini"\napi_key = "AIza-secret"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "api_key" in str(exc.value)


def test_selection_is_local_by_default(tmp_path: Path, monkeypatch) -> None:
    # The code default stays local for both query steps, so a checkout with
    # no key and no network still answers.
    monkeypatch.chdir(tmp_path)
    cfg = LoreConfig.load()
    assert cfg.selection.provider == "ollama"
    assert cfg.selection.models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


def test_selection_can_opt_into_gemini_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.selection]\nprovider = "gemini"\nmodels = ["gemini-3.5-flash-lite"]\n')
    cfg = LoreConfig.load(toml)
    assert cfg.selection.provider == "gemini"
    assert cfg.selection.models == ["gemini-3.5-flash-lite"]
    assert cfg.synthesis.provider == "ollama", "the two steps switch independently"


def test_selection_rejects_an_unknown_provider(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.selection]\nprovider = "openai"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "provider" in str(exc.value)


def test_an_api_key_in_the_selection_section_is_refused(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.selection]\nprovider = "gemini"\napi_key = "AIza-secret"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "api_key" in str(exc.value)


def test_the_committed_lore_toml_puts_both_query_steps_on_gemini() -> None:
    # The user's decision (Task 8b): quality over running locally. The code
    # defaults above stay "ollama"; only the committed project config opts in.
    cfg = LoreConfig.load(Path(__file__).parent.parent / "lore.toml")
    assert cfg.selection.provider == "gemini"
    assert cfg.synthesis.provider == "gemini"


# --- Task R1: Gemini for the graph build -----------------------------------

def test_extraction_provider_accepts_gemini_and_rejects_unknown(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore]\nextraction_provider = "gemini"\n')
    assert LoreConfig.load(toml).extraction_provider == "gemini"
    toml.write_text('[lore]\nextraction_provider = "openai"\n')
    with pytest.raises(Exception) as exc:
        LoreConfig.load(toml)
    assert "extraction_provider" in str(exc.value)


def test_gemini_extraction_models_default_to_the_lite_tier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = LoreConfig.load()
    assert cfg.extraction.gemini_models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


def test_adjudication_is_local_by_default_with_lite_models(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = LoreConfig.load()
    assert cfg.adjudication.provider == "ollama"
    assert cfg.adjudication.models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    assert cfg.adjudication_dir == Path("data") / "adjudication"


def test_adjudication_can_opt_into_gemini_and_refuses_unknowns(tmp_path: Path) -> None:
    import pytest

    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.adjudication]\nprovider = "gemini"\nmodels = ["gemini-3.5-flash-lite"]\n')
    cfg = LoreConfig.load(toml)
    assert cfg.adjudication.provider == "gemini"
    assert cfg.adjudication.models == ["gemini-3.5-flash-lite"]
    for bad in ('provider = "openai"', 'provider = "gemini"\napi_key = "AIza-secret"'):
        toml.write_text(f"[lore.adjudication]\n{bad}\n")
        with pytest.raises(Exception):
            LoreConfig.load(toml)


def test_the_committed_lore_toml_builds_the_graph_with_gemini() -> None:
    # The user's decision (Task R1): quality over running locally, for the
    # graph build too. Code defaults stay local.
    cfg = LoreConfig.load(Path(__file__).parent.parent / "lore.toml")
    assert cfg.extraction_provider == "gemini"
    assert cfg.adjudication.provider == "gemini"
    assert cfg.disambiguation.enabled is True


def test_processing_answer_reserve_default_and_toml(tmp_path) -> None:
    from lore.config import LoreConfig
    assert LoreConfig().processing.answer_reserve == 60
    p = tmp_path / "lore.toml"
    p.write_text("[lore.processing]\nanswer_reserve = 12\n")
    assert LoreConfig.load(p).processing.answer_reserve == 12
    assert LoreConfig.load(Path("lore.toml")).processing.answer_reserve == 60
