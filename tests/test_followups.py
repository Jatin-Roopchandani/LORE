"""Follow-up questions: the decomposer reads the last turns of the conversation
and rewrites the question to stand alone; that rewrite drives everything after.
No test reaches a model."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from lore.mcp import tools
from lore.query import agent as agent_module
from lore.query import decompose as decompose_module
from lore.query.decompose import (
    MAX_HISTORY_CHARS, MAX_HISTORY_TURNS, PartSpec, RecapSpec, Turn, active_schema, clip_history,
    decomposition_prompt, plan,
)
from tests.position_fixtures import HOLLOW, build_position_library
from tests.test_followups_golden import recording_models
from tests.precision_pins import before_prose_precision

HISTORY = [Turn("Who is Ren Ashby?", "Ren Ashby is a ranger who serves Dorn.")]


def _replying(standalone, parts=None, sink=None):
    async def generate(prompt: str, schema: type[BaseModel]):
        if sink is not None:
            sink.append((prompt, schema))
        fields = {"sub_questions": [PartSpec(question=p) for p in (parts or [standalone])],
                  "depth": "focused"}
        if "standalone" in schema.model_fields:
            fields["standalone"] = standalone
        return schema(**fields)
    return generate


# --- the history itself -------------------------------------------------------------

def test_history_keeps_the_last_three_turns_within_the_character_budget() -> None:
    turns = [Turn(f"question {i}?", "x" * 2000) for i in range(6)]
    kept = clip_history(turns)
    assert len(kept) == MAX_HISTORY_TURNS
    assert [t.question for t in kept] == ["question 3?", "question 4?", "question 5?"]
    assert all(len(t.answer) <= decompose_module.HISTORY_ANSWER_CHARS + 1 for t in kept)
    assert sum(len(t.question) + len(t.answer) for t in kept) <= MAX_HISTORY_CHARS


def test_history_drops_the_oldest_turns_past_the_budget_and_takes_dicts() -> None:
    long_q = "q" * 1400
    kept = clip_history([{"question": long_q, "answer": "a"},
                         {"question": long_q, "answer": "b"},
                         {"question": long_q, "answer": "c"}])
    assert [t.answer for t in kept] == ["b", "c"]
    assert clip_history(None) == () and clip_history([]) == ()
    assert clip_history([{"question": " ", "answer": "a"}]) == ()


# --- the prompt and the schema ------------------------------------------------------

def test_with_history_the_prompt_has_the_conversation_and_the_standalone_rule() -> None:
    prompt = decomposition_prompt("What did he do next?", ["Hollow One"], history=HISTORY)
    flat = " ".join(prompt.split())
    assert "Earlier in this conversation" in prompt
    assert "Who is Ren Ashby?" in prompt and "a ranger who serves Dorn" in prompt
    assert prompt.index("Earlier in this conversation") < prompt.index("Question: What did he do next?")
    assert 'Also return "standalone"' in flat
    assert "return it unchanged" in flat
    assert "standalone" in active_schema(followup=True).model_fields
    assert active_schema(followup=True).model_json_schema()["title"] == "Decomposition"


def test_without_history_the_prompt_and_schema_are_untouched() -> None:
    assert decomposition_prompt("Who?", ["A"]) == decomposition_prompt("Who?", ["A"], history=())
    assert "standalone" not in active_schema().model_fields
    assert "Earlier in this conversation" not in decomposition_prompt("Who?", ["A"])


def test_followups_off_ignores_history(monkeypatch) -> None:
    monkeypatch.setattr(decompose_module, "FOLLOWUPS", False)
    assert (decomposition_prompt("What next?", history=HISTORY)
            == decomposition_prompt("What next?"))


def test_the_rule_is_general_not_tuned_to_one_book() -> None:
    rule = decompose_module._STANDALONE_RULE.lower()
    for word in ("holmes", "watson", "sherlock", "baker"):
        assert word not in rule


# --- plan ---------------------------------------------------------------------------

async def test_plan_returns_the_standalone_question() -> None:
    sink: list = []
    got = await plan("What did he do next?",
                     _replying("What did Ren Ashby do after serving Dorn?", sink=sink),
                     history=HISTORY)
    assert got.standalone == "What did Ren Ashby do after serving Dorn?"
    assert got.questions == ["What did Ren Ashby do after serving Dorn?"]
    assert "Earlier in this conversation" in sink[0][0]
    assert "standalone" in sink[0][1].model_fields


@pytest.mark.parametrize("reply", ["", "   ", "who is  Mira Cole?", None])
async def test_a_blank_or_same_rewrite_is_no_rewrite(reply) -> None:
    got = await plan("Who is Mira Cole?", _replying(reply, parts=["Who is Mira Cole?"]),
                     history=HISTORY)
    assert got.standalone == ""


async def test_without_history_there_is_no_rewrite() -> None:
    got = await plan("Who is Mira Cole?", _replying("ignored"))
    assert got.standalone == ""


# --- the agent ----------------------------------------------------------------------

def _followup_models(standalone: str, parts=None):
    models, calls = recording_models(parts or [standalone])
    inner = models.decompose

    async def decompose(prompt, schema):
        reply = await inner(prompt, schema)
        if "standalone" in schema.model_fields:
            reply = schema(**reply.model_dump(), standalone=standalone)
        return reply

    import dataclasses
    return dataclasses.replace(models, decompose=decompose), calls


async def test_the_standalone_question_drives_retrieval_and_the_answer(tmp_path) -> None:
    config = build_position_library(tmp_path)
    standalone = "Whom does Ren Ashby trust after leaving Dorn?"
    models, calls = _followup_models(standalone)
    events: list[dict] = []
    result = await tools.lore_search(config, "Whom does he trust then?", models=models,
                                     world=HOLLOW, on_event=events.append,
                                     history=[{"question": "Who is Ren Ashby?",
                                               "answer": "A ranger who serves Dorn."}])
    assert "Earlier in this conversation" in calls["decompose"][0]["prompt"]
    assert "A ranger who serves Dorn." in calls["decompose"][0]["prompt"]
    # The rewrite is embedded and used; the words as typed go no further.
    embedded = [t for batch in calls["embed"] for t in batch]
    assert any(standalone in t for t in embedded)
    assert all("Whom does he trust then?" not in p for p in calls["select"] + calls["synthesize"])
    assert any(standalone in p for p in calls["synthesize"])
    understood = [e for e in events if e["type"] == "understood"]
    assert understood == [{"type": "understood", "standalone": standalone}]
    assert [e["type"] for e in events].index("understood") < [e["type"] for e in events].index("parts")
    assert result["standalone"] == standalone


async def test_an_unchanged_rewrite_sends_no_event(tmp_path) -> None:
    config = build_position_library(tmp_path)
    models, calls = _followup_models("Who is Ren Ashby?")
    events: list[dict] = []
    result = await tools.lore_search(config, "Who is Ren Ashby?", models=models, world=HOLLOW,
                                     on_event=events.append,
                                     history=[{"question": "Who is Mira Cole?", "answer": "x"}])
    assert not [e for e in events if e["type"] == "understood"]
    assert "standalone" not in result


@pytest.mark.parametrize("route", ["_recap", "_deep"])
async def test_the_standalone_question_drives_recap_and_deep_routing(tmp_path, monkeypatch,
                                                                     route) -> None:
    config = build_position_library(tmp_path)
    seen: list[str] = []

    async def capture(conn, kuzu, lance, question, *args, **kwargs):
        seen.append(question)
        return None  # fall through to the ordinary answer

    monkeypatch.setattr(agent_module, route, capture)
    standalone = "Recap the story since Ren Ashby left Dorn."
    models, calls = _followup_models(standalone)
    inner = models.decompose

    async def decompose(prompt, schema):
        reply = await inner(prompt, schema)
        return reply.model_copy(update={"depth": "deep",
                                        "recap": RecapSpec(span="story") if route == "_recap" else None})

    import dataclasses
    models = dataclasses.replace(models, decompose=decompose)
    await tools.lore_search(config, "And after that?", models=models, world=HOLLOW,
                            history=[{"question": "What did Ren do?", "answer": "He left Dorn."}])
    assert seen == [standalone]


# --- the eval arm and the trace -----------------------------------------------------

def test_run_pair_has_a_followups_arm() -> None:
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parents[1] / "evals/answer_quality/run_pair.py"
    spec = importlib.util.spec_from_file_location("run_pair_followups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert decompose_module.FOLLOWUPS is True
    assert module.parse_arm("followups=off") == {("lore.query.decompose", "FOLLOWUPS"): False}
    assert module.parse_arm("followups=on") == {("lore.query.decompose", "FOLLOWUPS"): True}


def test_the_trace_keeps_the_rewrite_with_the_decomposition() -> None:
    from lore import tracing
    trace = tracing.Trace(request_id="r1")
    trace.step({"type": "understood", "standalone": "Where did Ren Ashby go?", "t": 0.1})
    assert trace.decomposition["understood"] == {"standalone": "Where did Ren Ashby go?"}


async def test_followups_off_answers_as_without_history(tmp_path, monkeypatch) -> None:
    before_prose_precision(monkeypatch)  # pinned before prose-precision
    from tests.test_followups_golden import GOLDEN, capture
    import json
    monkeypatch.setattr(decompose_module, "FOLLOWUPS", False)
    config = build_position_library(tmp_path)
    got = await capture(config, history=[{"question": "Who is Mira Cole?", "answer": "x"}])
    assert got == json.loads(GOLDEN.read_text())
