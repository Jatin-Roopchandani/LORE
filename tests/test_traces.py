"""The trace store (release step 4): every /api/ask leaves a trace keyed by its
request id, failed answers too; secrets never enter; retention; cascades;
`lore trace`."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import BaseModel
from typer.testing import CliRunner

from lore import tracing
from lore.central import history as H
from lore.central import traces as T
from lore.central.models import Trace as TraceRow, User
from lore.config import LoreConfig
from lore.inference.gemini import GeminiError
from lore.web.app import create_app
from tests.auth_fixtures import FAKE_KEY, NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

RESULT = {"answer": "Holmes met Watson.", "mode": "graph", "claims": [], "passages": [],
          "edges": [], "seeds": [], "sub_questions": ["who?"], "answer_style": "claims"}


class Reply(BaseModel):
    text: str


@dataclasses.dataclass(frozen=True)
class Models:
    select: object
    synthesize: object
    synthesis_model: object
    selection_model: object
    decompose: object = None
    check: object = None
    write: object = None


def models(fail_synth: Exception | None = None, delay: float = 0.0):
    async def gen(prompt, schema):
        await asyncio.sleep(delay)
        return Reply(text=f"re {prompt}")

    async def synth(prompt, schema):
        await asyncio.sleep(delay)
        if fail_synth is not None:
            raise fail_synth
        return Reply(text=f"re {prompt}")
    return Models(select=gen, synthesize=synth, decompose=gen, synthesis_model=lambda: "flash",
                  selection_model=lambda: "lite")


def fake_search(fail_synth=None, delay=0.0, log_line=False):
    """Like lore_search: the models are wrapped for the bound trace."""
    async def search(config, query, models_=None, on_event=None, **_):
        m = tracing.maybe_traced(models(fail_synth, delay))
        on_event({"type": "parts", "sub_questions": [query]})
        await m.decompose(f"{query}/decompose", Reply)
        on_event({"type": "step", "name": "split", "state": "done", "part": None, "detail": "1 part"})
        await m.select(f"{query}/select", Reply)
        if log_line:
            logging.getLogger("lore.test").warning("inside the question")
        try:
            await m.synthesize(f"{query}/synthesize", Reply)
        except GeminiError as exc:
            return {"error": {"code": "upstream_unavailable", "message": str(exc)}}
        return RESULT
    return search


def ask(client, body):
    with client.stream("POST", "/api/ask", json=body) as r:
        text = b"".join(r.iter_bytes()).decode()
        headers = r.headers
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events, headers


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def app(root, central):
    return make_app(root, central)


@pytest.fixture
def alice(app, central):
    give_key(central, "alice")
    return sign_in(app, central, "alice")


def stored(central, rid) -> TraceRow:
    with central() as s:
        return s.get(TraceRow, rid)


def test_every_ask_is_traced_under_its_request_id(alice, central, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search())
    events, headers = ask(alice, {"question": "Who is Holmes?", "world": "all", "depth": "deep"})
    assert events[0][0] == "request"
    rid = events[0][1]["request_id"]
    assert headers["x-request-id"] == rid
    answer = dict(events)["answer"]
    row = stored(central, rid)
    assert row.user_id == "alice" and row.question == "Who is Holmes?"
    assert (row.world, row.depth) == ("all", "deep")
    assert row.chat_id == answer["chat_id"] and row.message_id == answer["message_id"]
    assert [c["stage"] for c in row.calls_json] == ["decompose", "select", "synthesize"]
    assert row.calls_json[0]["prompt"] == "Who is Holmes?/decompose"
    assert [s["type"] for s in row.steps_json] == ["parts", "step"]
    assert row.decomposition_json == {"parts": {"sub_questions": ["Who is Holmes?"]}}
    assert row.answer_json == RESULT and row.error is None
    assert row.timings_json["total_ms"] is not None
    assert row.model_usage_json == {"flash": {"requests": 2, "failed": 0},
                                    "lite": {"requests": 1, "failed": 0}}
    # the message links back to it
    msgs = alice.get(f"/api/chats/{answer['chat_id']}").json()["messages"]
    assert msgs[0]["request_id"] == rid


def test_a_failed_answer_is_traced_with_its_error(alice, central, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search",
                        fake_search(fail_synth=GeminiError("HTTP 500 from flash")))
    events, _ = ask(alice, {"question": "q"})
    row = stored(central, events[0][1]["request_id"])
    assert "upstream_unavailable" in row.error and row.answer_json is None
    assert row.calls_json[-1]["ok"] is False and "HTTP 500" in row.calls_json[-1]["error"]
    assert row.chat_id is None


def test_an_exception_is_traced(alice, central, monkeypatch):
    async def boom(config, query, on_event=None, **_):
        raise RuntimeError("kaboom")
    monkeypatch.setattr("lore.web.app.tools.lore_search", boom)
    events, _ = ask(alice, {"question": "q"})
    assert dict(events)["error"]["error"]["code"] == "internal"
    row = stored(central, events[0][1]["request_id"])
    assert "RuntimeError: kaboom" in row.error


def test_a_key_in_a_failing_call_never_reaches_the_trace(alice, central, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search(
        fail_synth=GeminiError(f"HTTP 400 for key={FAKE_KEY} session=abc")))
    events, _ = ask(alice, {"question": f"is {FAKE_KEY} here?"})
    rid = events[0][1]["request_id"]
    with central() as s:
        blob = json.dumps(T.trace_out(s.get(TraceRow, rid)))
    assert FAKE_KEY not in blob and "[redacted key]" in blob
    assert alice.session_token not in blob
    assert alice.cookies.get("lore_csrf") not in blob


def test_two_users_asking_at_once_get_two_separate_complete_traces(app, central, monkeypatch):
    give_key(central, "alice")
    give_key(central, "bob")
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search(delay=0.05))
    clients = {u: sign_in(app, central, u) for u in ("alice", "bob")}
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = {u: pool.submit(ask, c, {"question": f"{u}-question"})
                   for u, c in clients.items()}
        results = {u: f.result() for u, f in futures.items()}
    rids = {u: results[u][0][0][1]["request_id"] for u in results}
    assert rids["alice"] != rids["bob"]
    for user, rid in rids.items():
        row = stored(central, rid)
        assert row.user_id == user
        prompts = [c["prompt"] for c in row.calls_json]
        assert len(prompts) == 3 and all(p.startswith(f"{user}-question") for p in prompts)


def test_log_lines_carry_the_request_id(alice, central, monkeypatch, caplog):
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search(log_line=True))
    with caplog.at_level(logging.WARNING, logger="lore.test"):
        events, _ = ask(alice, {"question": "q"})
    rid = events[0][1]["request_id"]
    line = next(r for r in caplog.records if "inside the question" in r.getMessage())
    assert line.request_id == rid and rid in line.getMessage()


def test_every_response_has_a_request_id(alice):
    r = alice.get("/api/chats", headers={"X-Request-Id": "client-chosen"})
    assert r.headers["x-request-id"] != "client-chosen" and len(r.headers["x-request-id"]) == 36


# --- retention and cascades ------------------------------------------------------

def _row(s, rid, user="alice", chat=None, at=NOW):
    t = tracing.Trace(request_id=rid)
    t.finish(answer={"answer": "x"})
    T.save(s, t, user_id=user, question="q", chat_id=chat, now=at)


def test_traces_older_than_30_days_are_purged(central):
    with central() as s:
        _row(s, "old", at=NOW - timedelta(days=31))
        _row(s, "new", at=NOW - timedelta(days=29))
        s.commit()
        assert T.purge_expired(s, NOW) == 1
        s.commit()
        assert s.get(TraceRow, "old") is None and s.get(TraceRow, "new") is not None


def test_the_worker_purges_expired_traces(central, root):
    from lore.worker import Worker
    with central() as s:
        _row(s, "old", at=NOW - timedelta(days=40))
        s.commit()
    Worker(root, central, clock=lambda: NOW).run_once()
    assert stored(central, "old") is None


def test_multi_mode_purges_at_start_up(root, central):
    with central() as s:
        _row(s, "ancient", at=NOW - timedelta(days=400))
        s.commit()
    make_app(root, central)
    assert stored(central, "ancient") is None


def test_deleting_a_chat_deletes_its_traces(central):
    with central() as s:
        chat, _ = H.create_chat(s, "alice", title="t", now=NOW)
        _row(s, "r1", chat=chat.id)
        _row(s, "r2")
        s.commit()
        H.delete_chat(s, "alice", chat.id, NOW)
        s.commit()
        assert s.get(TraceRow, "r1") is None and s.get(TraceRow, "r2") is not None


def test_purging_a_chat_over_the_limit_deletes_its_traces(central, monkeypatch):
    monkeypatch.setattr(H, "MAX_CHATS", 1)
    with central() as s:
        chat, _ = H.create_chat(s, "alice", title="t", now=NOW)
        _row(s, "r1", chat=chat.id)
        s.commit()
        H.create_chat(s, "alice", title="t2", now=NOW + timedelta(minutes=1))
        s.commit()
        assert s.get(TraceRow, "r1") is None


def test_deleting_the_account_deletes_its_traces(central):
    with central() as s:
        _row(s, "r1")
        _row(s, "r2", user="bob")
        s.commit()
        s.execute(sa.delete(User).where(User.id == "alice"))
        s.commit()
        assert s.get(TraceRow, "r1") is None and s.get(TraceRow, "r2") is not None


# --- local mode and lore trace ---------------------------------------------------

def test_local_mode_traces_without_sign_in_and_lore_trace_prints_it(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.setattr("lore.web.app.tools.lore_search", fake_search())
    config = LoreConfig(data_dir=tmp_path)
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8766")
    events, _ = ask(client, {"question": "Who is Holmes?"})
    rid = events[0][1]["request_id"]
    assert dict(events)["answer"]["chat_id"]

    from lore.cli import app as cli
    monkeypatch.setattr("lore.cli.LoreConfig.load", classmethod(lambda cls, *a, **k: config))
    out = CliRunner().invoke(cli, ["trace", rid])
    assert out.exit_code == 0, out.output
    assert "Question: Who is Holmes?" in out.output
    assert "1. decompose on flash" in out.output and "Holmes met Watson." in out.output
    assert json.loads(CliRunner().invoke(cli, ["trace", rid, "--json"]).output)["request_id"] == rid
    missing = CliRunner().invoke(cli, ["trace", "nope"])
    assert missing.exit_code == 1
