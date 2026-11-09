"""Stop: a question the reader stops makes no further model call, frees its
slots at once, is not saved as an answer, and leaves a trace marked stopped.
Only the asker can stop it (POST /api/ask/{request_id}/cancel, CSRF-checked)."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
import sqlalchemy as sa

from lore import tracing
from lore.central import traces as trace_store
from lore.central.models import InflightSlot
from lore.config import LoreConfig
from lore.web.app import create_app
from lore.web.ask import STOPPED, ask_events
from lore.web.throttle import RateSettings
from tests.auth_fixtures import BASE_URL, NOW, Clock, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

RESULT = {"answer": "ok", "mode": "graph", "claims": [], "passages": [],
          "edges": [], "seeds": [], "sub_questions": ["who?"]}
CALL_S = 0.05   # one fake model call
CALLS = 200     # a whole answer: 10 s of calls, far longer than any test waits


def parse(body: bytes) -> list[tuple[str, dict]]:
    out = []
    for block in body.decode().strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


class FakeModel:
    """A model whose every call takes CALL_S; `calls` holds when each started."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.started = threading.Event()

    async def generate(self) -> str:
        self.calls.append(time.monotonic())
        self.started.set()
        await asyncio.sleep(CALL_S)
        return "reply"


def pipeline(model: FakeModel, seen: dict | None = None):
    """lore_search's shape: steps streamed, then model call after model call."""
    async def search(config, query, on_event=None, **_):
        if seen is not None:
            seen["rid"] = tracing.current_request_id()
        on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
        for _ in range(CALLS):
            await model.generate()
        return RESULT
    return search


# --- the stream itself -------------------------------------------------------

async def test_stop_cancels_mid_pipeline_and_no_model_call_follows():
    model, stop, lock = FakeModel(), asyncio.Event(), asyncio.Lock()
    finished = []
    gen = ask_events(None, "q", lock, search=pipeline(model), stop=stop, request_id="r1",
                     on_finish=lambda result, error, events: finished.append((result, error)))
    chunks = [await gen.__anext__(), await gen.__anext__()]   # request, step
    while len(model.calls) < 3:
        await asyncio.sleep(0.01)
    stop.set()
    chunks += [chunk async for chunk in gen]
    at_stop = len(model.calls)
    await asyncio.sleep(CALL_S * 4)
    assert len(model.calls) == at_stop < CALLS               # nothing after the stop
    events = parse(b"".join(chunks))
    assert [e for e, _ in events] == ["request", "step", "stopped"]
    assert finished == [(None, STOPPED)]                     # not an answer: never saved as one
    assert not lock.locked()
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_a_disconnect_mid_pipeline_also_stops_the_model_calls():
    model, lock = FakeModel(), asyncio.Lock()
    finished = []

    async def consume():
        async for _ in ask_events(None, "q", lock, search=pipeline(model),
                                  on_finish=lambda r, e, ev: finished.append(e)):
            pass

    consumer = asyncio.create_task(consume())
    while len(model.calls) < 2:
        await asyncio.sleep(0.01)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    at_cancel = len(model.calls)
    await asyncio.sleep(CALL_S * 4)
    assert len(model.calls) == at_cancel
    assert [e["code"] for e in finished] == ["cancelled"]
    assert not lock.locked()


async def test_a_stop_after_the_answer_changes_nothing():
    stop = asyncio.Event()

    async def quick(config, query, on_event=None, **_):
        return RESULT
    chunks = [c async for c in ask_events(None, "q", asyncio.Lock(), search=quick, stop=stop)]
    stop.set()
    assert [e for e, _ in parse(b"".join(chunks))] == ["answer"]
    assert asyncio.all_tasks() == {asyncio.current_task()}


# --- the endpoint (multi mode) -----------------------------------------------

@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    for user in ("alice", "bob"):
        give_key(sessions, user)
    return sessions


def slots(central):
    with central() as s:
        return s.scalars(sa.select(InflightSlot)).all()


def start_asking(client, out: dict) -> threading.Thread:
    """POST /api/ask on its own thread (TestClient reads a stream whole)."""
    def run():
        out["response"] = client.post("/api/ask", json={"question": "who?"})
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_the_owner_stops_the_answer_and_its_slot_is_freed(tmp_path, central, monkeypatch):
    model, seen = FakeModel(), {}
    monkeypatch.setattr("lore.web.app.tools.lore_search", pipeline(model, seen))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), central, auth_clock=Clock(NOW),
                   rate_settings=RateSettings(ask_per_minute=0))
    asker, alice, bob = (sign_in(app, central, u) for u in ("alice", "alice", "bob"))
    out: dict = {}
    thread = start_asking(asker, out)
    assert model.started.wait(10)
    rid = seen["rid"]
    assert rid and len(slots(central)) > 0                  # the question holds its slots

    # Someone else's question, or no question at all: the same 404.
    assert bob.post(f"/api/ask/{rid}/cancel").status_code == 404
    assert alice.post("/api/ask/not-a-question/cancel").status_code == 404
    # No CSRF token: refused before anything is looked up.
    bare = sign_in(app, central, "alice")
    del bare.headers["X-CSRF-Token"]
    assert bare.post(f"/api/ask/{rid}/cancel").status_code == 403
    assert thread.is_alive()                                # none of those stopped it

    r = alice.post(f"/api/ask/{rid}/cancel")
    assert r.status_code == 200 and r.json() == {"cancelled": True}
    thread.join(10)
    assert not thread.is_alive()
    at_stop = len(model.calls)
    time.sleep(CALL_S * 4)
    assert len(model.calls) == at_stop < CALLS              # no further model call
    events = parse(out["response"].content)
    assert events[0][0] == "request" and events[-1][0] == "stopped"
    assert slots(central) == []                             # the in-flight slot is back
    assert alice.post(f"/api/ask/{rid}/cancel").status_code == 404   # over: nothing to stop
    with central() as s:
        row = trace_store.get(s, rid)
        assert row is not None and row.user_id == "alice"
        assert row.error.startswith("stopped")              # the trace says it was stopped
        assert row.message_id is None                       # not saved as a turn
    # The next question runs at once (the slot, the lock and the throttle are free).
    monkeypatch.setattr("lore.web.app.tools.lore_search",
                        lambda config, query, on_event=None, **_: _answer())
    again = parse(alice.post("/api/ask", json={"question": "who?"}).content)
    assert again[-1][0] == "answer"


async def _answer():
    return RESULT


def test_local_mode_has_the_cancel_route_too(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.mcp_fixtures import build_store_triple
    config = build_store_triple(tmp_path, books=[("b1", "A Study")])
    client = TestClient(create_app(config), base_url=BASE_URL)
    assert client.post("/api/ask/nope/cancel").status_code == 404
