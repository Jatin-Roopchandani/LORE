"""LORE's viewpoint: the packaged policy, a configured replacement, and the
loader's errors."""
from __future__ import annotations

import pytest

from lore.config import LoreConfig
from lore.query.viewpoint import DEFAULT_VIEWPOINT, ViewpointError, load_viewpoint


def test_the_packaged_policy_is_the_default() -> None:
    text = load_viewpoint()
    assert text == DEFAULT_VIEWPOINT.read_text(encoding="utf-8").strip()
    assert load_viewpoint(None) == load_viewpoint("") == text


def test_the_policy_states_the_careful_critics_rules() -> None:
    text = " ".join(load_viewpoint().split()).lower()
    for rule in ("take a position when the evidence leans one way",
                 "on balance", "the evidence is split",
                 "keep what the text says separate from what we read into it",
                 "strongest case for the other side",
                 "never present an inference as something a book says",
                 "within the story's own world and values first",
                 "never fill the gap from outside knowledge",
                 "prefer what characters do over what is said about them",
                 "too thin for a position"):
        assert rule in text, rule


def test_a_configured_file_replaces_the_default(tmp_path) -> None:
    custom = tmp_path / "mine.md"
    custom.write_text("\n  Always be brief.  \n", encoding="utf-8")
    assert load_viewpoint(custom) == "Always be brief."
    assert load_viewpoint(str(custom)) == "Always be brief."


def test_the_config_names_the_file_and_defaults_to_none(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert LoreConfig.load().answers.viewpoint is None
    toml = tmp_path / "lore.toml"
    toml.write_text('[lore.answers]\nviewpoint = "policy.md"\n')
    (tmp_path / "policy.md").write_text("Be fair.")
    cfg = LoreConfig.load(toml)
    assert str(cfg.answers.viewpoint) == "policy.md"
    assert load_viewpoint(cfg.answers.viewpoint) == "Be fair."  # relative to the working directory


def test_a_missing_or_empty_file_is_an_error_not_a_silent_default(tmp_path) -> None:
    with pytest.raises(ViewpointError, match="cannot read"):
        load_viewpoint(tmp_path / "absent.md")
    (tmp_path / "empty.md").write_text("  \n")
    with pytest.raises(ViewpointError, match="empty"):
        load_viewpoint(tmp_path / "empty.md")
    assert issubclass(ViewpointError, ValueError)
