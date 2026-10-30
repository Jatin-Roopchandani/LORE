"""`depth` (auto | deep | focused) and the decomposition's kinds and lenses,
from the question down to answer_question, over the synthetic worlds library
(tests/worlds_fixtures.py). Models are scripted; no test reaches one."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lore.mcp import tools
from lore.mcp.stores import open_stores
from lore.query import decompose as decompose_module
from lore.query.agent import QueryModels, answer_question
from lore.query.decompose import Decomposition, PartSpec, SubQuestions
from lore.query.select import TemplateChoice
from lore.query.synthesize import Synthesis
from lore.web.app import create_app
from tests.worlds_fixtures import ALPHA, QUESTION_VECTOR, build_library

QUESTION = "Who is Zed Quill?"


def scripted(decomposition):
    """Models whose decomposition returns `decomposition`, recording the calls."""
    calls = {"decompose": [], "synthesize": []}

    async def embed(texts):
        return [QUESTION_VECTOR for _ in texts]

    async def decompose(prompt, schema):
        calls["decompose"].append((prompt, schema))
        return decomposition

    async def select(prompt, schema):
        return TemplateChoice(template="neighbours", entities=[1])

    async def synthesize(prompt, schema):
        calls["synthesize"].append(prompt)
        return Synthesis(claims=[], can_answer=False)

    async def aclose():
        return None

    return QueryModels(embed, select, synthesize, lambda: "writer", aclose,
                       lambda: "chooser", decompose=decompose), calls


async def parts_event(config, decomposition, **kw):
    models, calls = scripted(decomposition)
    events = []
    with open_stores(config) as s:
        await answer_question(s.conn, s.kuzu, s.lance, QUESTION, models,
                              world=ALPHA, on_event=events.append, **kw)
    (event,) = [e for e in events if e["type"] == "parts"]
    return event, calls


INTERPRETIVE_PLAN = Decomposition(depth="deep", sub_questions=[
    PartSpec(question="Who is Zed Quill?"),
    PartSpec(question="Was Zed Quill right?", kind="interpretive",
             lenses=["What did Zed Quill do?", "Why did Zed Quill do it?"])])


@pytest.fixture
def config(tmp_path):
    return build_library(tmp_path)


async def test_the_parts_event_carries_kinds_lenses_and_the_planned_depth(config) -> None:
    event, calls = await parts_event(config, INTERPRETIVE_PLAN)
    assert event["sub_questions"] == ["Who is Zed Quill?", "Was Zed Quill right?"]
    assert event["kinds"] == ["factual", "interpretive"]
    assert event["lenses"] == [[], ["What did Zed Quill do?", "Why did Zed Quill do it?"]]
    assert event["depth"] == "deep"
    assert calls["decompose"][0][1] is Decomposition


@pytest.mark.parametrize("asked, expected", [
    ("auto", "deep"), ("deep", "deep"), ("focused", "focused")])
async def test_depth_override(config, asked, expected) -> None:
    event, _ = await parts_event(config, INTERPRETIVE_PLAN, depth=asked)
    assert event["depth"] == expected


async def test_auto_is_the_default_and_a_focused_plan_stays_focused(config) -> None:
    plan = Decomposition(sub_questions=[PartSpec(question=QUESTION)], depth="focused")
    assert (await parts_event(config, plan))[0]["depth"] == "focused"
    assert (await parts_event(config, plan, depth="deep"))[0]["depth"] == "deep"


async def test_a_failed_decomposition_is_one_factual_focused_part(config) -> None:
    event, _ = await parts_event(config, Synthesis(claims=[], can_answer=False))
    assert (event["kinds"], event["lenses"], event["depth"]) == (["factual"], [[]], "focused")
    # ... and an override still applies
    event, _ = await parts_event(config, Synthesis(claims=[], can_answer=False), depth="deep")
    assert event["depth"] == "deep"


async def test_an_unknown_depth_is_refused_before_anything_runs(config) -> None:
    models, calls = scripted(INTERPRETIVE_PLAN)
    with open_stores(config) as s, pytest.raises(ValueError, match="depth"):
        await answer_question(s.conn, s.kuzu, s.lance, QUESTION, models, depth="shallow")
    assert calls["decompose"] == []


async def test_with_both_switches_off_the_event_and_schema_are_as_before(
        config, monkeypatch) -> None:
    monkeypatch.setattr(decompose_module, "INTERPRETIVE", False)
    monkeypatch.setattr(decompose_module, "DEEP", False)
    event, calls = await parts_event(config, SubQuestions(sub_questions=["Who is Zed Quill?"]),
                                     depth="deep")
    assert set(event) == {"type", "sub_questions"}
    assert calls["decompose"][0][1] is SubQuestions


# --- MCP ---------------------------------------------------------------------------

async def test_lore_search_passes_depth_only_when_given(config, monkeypatch) -> None:
    seen = []

    async def fake_answer(conn, kuzu, lance, query, models, **kw):
        seen.append(kw)
        raise RuntimeError("stop")  # no answer needed: only the arguments matter

    monkeypatch.setattr(tools, "answer_question", fake_answer)
    for kwargs in ({}, {"depth": "deep"}, {"depth": "auto", "world": ALPHA}):
        models, _ = scripted(INTERPRETIVE_PLAN)
        with pytest.raises(RuntimeError):
            await tools.lore_search(config, QUESTION, models=models, **kwargs)
    assert seen[0].get("depth") is None and "depth" not in seen[0]
    assert seen[1]["depth"] == "deep"
    assert seen[2] == {"on_event": None, "world": ALPHA, "depth": "auto"}


async def test_lore_search_deep_reaches_the_agent(config) -> None:
    models, calls = scripted(Decomposition(sub_questions=[PartSpec(question=QUESTION)]))
    events = []
    result = await tools.lore_search(config, QUESTION, models=models, world=ALPHA,
                                     depth="deep", on_event=events.append)
    assert "error" not in result
    assert [e["depth"] for e in events if e["type"] == "parts"] == ["deep"]


def test_the_mcp_tool_declares_depth_with_a_default() -> None:
    import asyncio

    from lore.config import LoreConfig
    from lore.mcp.server import build_server

    async def schema():
        listed = await build_server(LoreConfig()).list_tools()
        return next(t for t in listed if t.name == "lore_search").input_schema

    props = asyncio.run(schema())["properties"]
    assert props["depth"]["enum"] == ["auto", "deep", "focused"]
    assert props["depth"]["default"] == "auto"


# --- web ---------------------------------------------------------------------------

@pytest.fixture
def client(config):
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_ask_passes_depth_to_the_search_only_when_set(client, monkeypatch) -> None:
    seen = []

    async def fake_search(config, question, on_event=None, **kw):
        seen.append(kw)
        return {"answer": "x"}

    monkeypatch.setattr("lore.mcp.tools.lore_search", fake_search)
    for body in ({"question": "q"}, {"question": "q", "depth": "auto"},
                 {"question": "q", "depth": "deep"},
                 {"question": "q", "world": ALPHA, "depth": "focused"}):
        assert client.post("/api/ask", json=body).status_code == 200
    assert seen == [{}, {}, {"depth": "deep"}, {"world": ALPHA, "depth": "focused"}]


def test_ask_refuses_an_unknown_depth(client) -> None:
    assert client.post("/api/ask", json={"question": "q", "depth": "shallow"}).status_code == 400
