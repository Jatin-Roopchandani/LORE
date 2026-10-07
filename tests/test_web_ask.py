"""POST /api/ask: events stream in order, errors pass through, one question
at a time, and closing the stream stops the run (no more Gemini spend)."""
import asyncio
import json
import math

import pytest
from fastapi.testclient import TestClient

from lore.web.ask import ask_events, sse
from lore.web.app import create_app
from tests.mcp_fixtures import build_store_triple

RESULT = {"answer": "Holmes met Watson.", "mode": "graph", "claims": [],
          "passages": [], "edges": [], "seeds": [], "sub_questions": ["who?"]}


def parse(chunks: list[bytes]) -> list[tuple[str, dict]]:
    out = []
    for block in b"".join(chunks).decode().strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def fake_search(config, query, models=None, on_event=None):
    on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
    on_event({"type": "parts", "sub_questions": ["who?"]})
    await asyncio.sleep(0)
    return RESULT


async def collect(gen):
    return [chunk async for chunk in gen]


def test_sse_framing_refuses_nan():
    assert sse("parts", {"t": 0.0, "sub_questions": []}).startswith(b"event: parts\ndata: ")
    with pytest.raises(ValueError):
        sse("x", {"v": math.nan})


async def test_events_then_the_answer():
    events = parse(await collect(ask_events(None, "who?", asyncio.Lock(), search=fake_search)))
    assert [e for e, _ in events] == ["step", "parts", "answer"]
    assert events[1][1]["sub_questions"] == ["who?"]
    assert events[-1][1]["result"] == RESULT
    assert all("t" in d for _, d in events)


async def test_a_tool_error_becomes_an_error_event():
    async def failing(config, query, models=None, on_event=None):
        return {"error": {"code": "budget_exhausted", "message": "spent"}}
    events = parse(await collect(ask_events(None, "q", asyncio.Lock(), search=failing)))
    assert events == [("error", {"t": events[0][1]["t"],
                                 "error": {"code": "budget_exhausted", "message": "spent"}})]


async def test_a_second_question_while_one_runs_is_busy():
    lock = asyncio.Lock()
    await lock.acquire()
    events = parse(await collect(ask_events(None, "q", lock, search=fake_search)))
    assert events[0][0] == "error" and events[0][1]["error"]["code"] == "busy"


async def test_closing_the_stream_cancels_the_run():
    calls_after = []

    async def slow(config, query, models=None, on_event=None):
        on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
        try:
            await asyncio.sleep(10)          # stands in for a Gemini call
            calls_after.append("spent")      # must never run
        except asyncio.CancelledError:
            calls_after.append("cancelled")
            raise
        return RESULT

    lock = asyncio.Lock()
    gen = ask_events(None, "q", lock, search=slow)
    first = await gen.__anext__()
    assert b"event: step" in first
    await gen.aclose()                       # generator-close path (the disconnect path is tested below)
    await asyncio.sleep(0)
    assert calls_after == ["cancelled"]
    assert not lock.locked()


async def test_a_crashing_search_ends_with_an_internal_error():
    async def boom(config, query, models=None, on_event=None):
        raise RuntimeError("kuzu exploded")
    lock = asyncio.Lock()
    events = parse(await collect(ask_events(None, "q", lock, search=boom)))
    assert events[-1][0] == "error" and events[-1][1]["error"]["code"] == "internal"
    assert not lock.locked()


async def test_a_disconnect_cancels_the_run_and_leaves_no_tasks():
    seen = []

    async def slow(config, query, models=None, on_event=None):
        on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    lock = asyncio.Lock()

    async def consume():
        async for _ in ask_events(None, "q", lock, search=slow):
            pass

    consumer = asyncio.create_task(consume())
    for _ in range(5):
        await asyncio.sleep(0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)
    assert seen == ["cancelled"]
    assert not lock.locked()
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_events_with_infinite_scores_are_valid_json():
    async def search(config, query, models=None, on_event=None):
        # emit() makes payloads safe; this checks the web layer keeps them so.
        from lore.query.events import emit
        from lore.query.models import SeedEntity
        emit(on_event, "seeds", part=0, entities=[SeedEntity("e1", "H", "Character", ("b1",), math.inf)])
        return RESULT
    events = parse(await collect(ask_events(None, "q", asyncio.Lock(), search=search)))
    assert events[0][1]["entities"][0]["score"] is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = build_store_triple(tmp_path, books=[("b1", "A Study")])
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search)
    return TestClient(create_app(config), base_url="http://127.0.0.1:8766")


def test_the_endpoint_streams_server_sent_events(client):
    with client.stream("POST", "/api/ask", json={"question": "who?"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert [e for e, _ in parse([body])] == ["step", "parts", "answer"]


@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_bad_questions_are_400(client, question):
    assert client.post("/api/ask", json={"question": question}).status_code == 400


def test_a_form_post_is_refused(client):
    r = client.post("/api/ask", data={"question": "who?"})
    assert r.status_code in (400, 415, 422)


def test_the_app_refuses_a_foreign_origin(client):
    r = client.post("/api/ask", json={"question": "who?"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
