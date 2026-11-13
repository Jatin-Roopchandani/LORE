"""Disambiguation, beats and codex with no model configured (local mode, no
key, no self-hosted server): each skips its requests with a note, and the book
finishes graphed with everything else intact instead of failing. With a model
configured nothing changes. Offline."""
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference import key_scope
from lore.inference.key_scope import KeyScope
from lore.orchestrator import jobs, plan
from lore.orchestrator.pipeline import run_book
from lore.orchestrator.runner import MarksGraphed
from lore.orchestrator.stages import (
    BeatsStage,
    CodexStage,
    DisambiguateStage,
    LoadStage,
    no_model_note,
)
from lore.resolve.disambiguation import DisambiguationStore
from tests.fixtures import seed_book
from tests.test_stages import BOOK, _Client, _questions

BEATS_NOTE = "skipped: no model configured for story beats"


@pytest.fixture
def no_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
                 "OPENAI_API_KEY", "LORE_LLM_API_KEY", "LORE_MULTI_USER"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def env(no_keys, monkeypatch):
    monkeypatch.setenv("LORE_DATA_DIR", str(no_keys / "data"))
    monkeypatch.chdir(no_keys)
    seed_book(BOOK)
    config = LoreConfig.load()
    conn = open_db(config.sqlite_path)
    yield config, conn
    conn.close()


def _llm(config, **llm):
    return config.model_copy(update={"llm": config.llm.model_validate(llm)})


def test_the_note_says_when_nothing_can_answer(no_keys, monkeypatch):
    cfg = LoreConfig()
    assert no_model_note(cfg, "story beats") == BEATS_NOTE
    assert no_model_note(_llm(cfg, provider="anthropic"), "story beats") == BEATS_NOTE
    assert no_model_note(_llm(cfg, provider="openai", model="m"), "entity profiles") == (
        "skipped: no model configured for entity profiles")
    local = _llm(cfg, provider="openai_compatible", base_url="http://localhost:8080/v1",
                 model="qwen")
    assert no_model_note(local, "story beats") is None
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake0000")
    assert no_model_note(cfg, "story beats") is None
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-FAKEfakeFAKEfakeFAKE-0001")
    assert no_model_note(_llm(cfg, provider="anthropic"), "story beats") is None


def test_multi_mode_never_skips(no_keys, monkeypatch):
    # A user without a key waits for one (KeyStop), as before.
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    assert no_model_note(LoreConfig(), "story beats") is None
    monkeypatch.delenv("LORE_MULTI_USER")
    with key_scope.bound(KeyScope("u", None, provider="anthropic")):
        assert no_model_note(LoreConfig(), "story beats") is None


def test_disambiguation_skips_its_questions_and_the_book_finishes(env):
    config, conn = env
    config = config.model_copy(update={
        "disambiguation": config.disambiguation.model_copy(update={"enabled": True})})
    store = DisambiguationStore(config.disambiguation_dir)
    for q in _questions(3):
        store.record(q)
    store.flush(BOOK)
    stage = DisambiguateStage()  # the real check, the real client factory
    job = run_book(conn, config, BOOK, [stage])
    assert job.state == jobs.DONE, job.stopped_reason
    assert stage.note == "skipped: no model configured for name disambiguation"
    store = DisambiguationStore(config.disambiguation_dir)
    assert len(store.unanswered(store.pending())) == 3  # still pending for later


def test_with_a_model_configured_disambiguation_asks_as_before(env):
    config, conn = env
    config = config.model_copy(update={
        "disambiguation": config.disambiguation.model_copy(update={"enabled": True})})
    store = DisambiguationStore(config.disambiguation_dir)
    for q in _questions(3):
        store.record(q)
    store.flush(BOOK)
    client = _Client()
    stage = DisambiguateStage(lambda cfg: client, model_note=lambda cfg: None)
    assert run_book(conn, config, BOOK, [stage]).state == jobs.DONE
    assert client.calls == 1 and stage.note is None


class _Graph:
    """A graph with no beats or codex work: enough for the pipeline's end."""

    def execute(self, *a, **k):
        return []

    def close(self):
        pass


def test_a_keyless_book_finishes_graphed_through_the_real_pipeline_end(env):
    """The stages as default_stages builds them (no factories injected), after
    a load: the book ends `graphed` and `done`, not failed."""
    config, conn = env
    config = config.model_copy(update={
        "disambiguation": config.disambiguation.model_copy(update={"enabled": True})})
    store = DisambiguationStore(config.disambiguation_dir)
    for q in _questions(2):
        store.record(q)
    store.flush(BOOK)
    stages = [LoadStage(lambda cfg, c, b: "loaded"), DisambiguateStage(reload=lambda ctx: None),
              BeatsStage(), CodexStage()]
    stages = [*stages[:-1], MarksGraphed(stages[-1])]
    job = run_book(conn, config, BOOK, stages)
    assert job.state == jobs.DONE, job.stopped_reason
    status = conn.execute("SELECT status FROM books WHERE id = ?", (BOOK,)).fetchone()[0]
    assert status == "graphed"


def test_the_step_list_shows_the_skip_with_its_note(env):
    from lore.cli import default_stages
    config, conn = env
    stages = default_stages(config, resume=True)
    notes = plan.stage_notes(stages, config)
    assert notes == {"beats": BEATS_NOTE,
                     "codex": "skipped: no model configured for entity profiles"}
    names = plan.stage_names(stages, config)
    jobs.start_job(conn, BOOK, "load", 1, now=jobs._aware(None))
    steps = plan.steps(conn, BOOK, jobs.get_job(conn, BOOK), names, jobs._aware(None),
                       notes=notes)["steps"]
    by = {s["stage"]: s for s in steps}
    assert by["load"]["state"] == "current"
    assert by["beats"] == {"stage": "beats", "state": "skipped", "note": BEATS_NOTE,
                           "seconds_left": 0}
    assert by["codex"]["state"] == "skipped"
    local = _llm(config, provider="openai_compatible", base_url="http://h:1/v1", model="q")
    assert plan.stage_notes(default_stages(local, resume=True), local) == {}
