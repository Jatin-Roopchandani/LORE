"""Follow-ups on the server: /api/ask with a chat_id loads that chat's last
turns from the central DB, for the signed-in user only, and passes them to the
search as history. A client can never send history text itself. The rewrite
is stored on the message and in the trace."""
from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central import db as central_db
from lore.central import history as H
from lore.central.models import Trace
from lore.config import LoreConfig
from lore.web.app import create_app
from lore.web.history import history_turns
from tests.auth_fixtures import NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions
from tests.test_history import ask

STANDALONE = "What did Ren Ashby do after the siege?"


def result(answer="Ren left.", short=None):
    out = {"answer": answer, "mode": "graph", "claims": [], "passages": [], "edges": [],
           "seeds": [], "sub_questions": ["q"], "answer_style": "claims"}
    if short:
        out["short_answer"] = [{"text": t, "claims": [], "passages": []} for t in short]
    return out


def recording_search(seen: list, rewrite: str | None = None):
    async def search(config, query, models=None, on_event=None, **kwargs):
        seen.append({"query": query, **kwargs})
        if rewrite:
            on_event({"type": "understood", "standalone": rewrite})
        on_event({"type": "parts", "sub_questions": [rewrite or query]})
        await asyncio.sleep(0)
        out = result(f"answer to {query}")
        if rewrite:
            out["standalone"] = rewrite
        return out
    return search


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def app(tmp_path, central):
    return make_app(LoreConfig(data_dir=tmp_path / "root"), central)


@pytest.fixture
def alice(app, central):
    give_key(central, "alice")
    return sign_in(app, central, "alice")


@pytest.fixture
def bob(app, central):
    give_key(central, "bob")
    return sign_in(app, central, "bob")


# --- the turns a chat gives -----------------------------------------------------------

def _messages(central, user, items, world=None):
    with central() as s:
        chat = None
        for question, payload, standalone in items:
            chat, msg = H.append_message(s, user, chat.id if chat else None, question=question,
                                         answer=payload, depth="auto", world=world,
                                         request_id=None, now=NOW, standalone=standalone)
        s.commit()
        return chat.id


def test_turns_are_the_last_three_with_short_answers_and_rewrites(central):
    long = "y" * 5000
    chat_id = _messages(central, "alice", [
        ("q1", {"result": result("a1"), "steps": []}, None),
        ("q2", {"result": result(long), "steps": []}, None),
        ("And then?", {"result": result("x", short=["Short one.", "Short two."]), "steps": []},
         "What happened after q2?"),
        ("q4", {"result": result("a4"), "steps": []}, None),
    ])
    with central() as s:
        _, messages = H.get_chat(s, "alice", chat_id)
    turns = history_turns(messages, world=None)
    assert [t["question"] for t in turns] == ["q2", "What happened after q2?", "q4"]
    assert turns[1]["answer"] == "Short one. Short two."
    assert len(turns[0]["answer"]) <= 601
    assert sum(len(t["question"]) + len(t["answer"]) for t in turns) <= 3000


def test_turns_skip_other_worlds_and_unkept_answers(central):
    chat_id = _messages(central, "alice", [
        ("in world 1", {"result": result("a1"), "steps": []}, None),
        ("too big", {"truncated": True, "bytes": 2_000_000}, None),
    ], world="1")
    with central() as s:
        _, messages = H.get_chat(s, "alice", chat_id)
    assert history_turns(messages, world="2") == []
    assert history_turns(messages, world=1) == [{"question": "in world 1", "answer": "a1"}]


# --- /api/ask ------------------------------------------------------------------------

def test_a_follow_up_gets_its_chats_turns_from_the_server(alice, monkeypatch):
    seen: list = []
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen))
    events, _ = ask(alice, {"question": "Who is Ren Ashby?"})
    chat_id = dict(events)["answer"]["chat_id"]
    assert "history" not in seen[0]  # a new chat: no history, nothing passed
    seen.clear()
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen, STANDALONE))
    events, _ = ask(alice, {"question": "What did he do next?", "chat_id": chat_id})
    assert seen[0]["history"] == [{"question": "Who is Ren Ashby?",
                                   "answer": "answer to Who is Ren Ashby?"}]
    assert seen[0]["query"] == "What did he do next?"
    kinds = [k for k, _ in events]
    assert "understood" in kinds and dict(events)["understood"]["standalone"] == STANDALONE
    msgs = alice.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["standalone"] for m in msgs] == [None, STANDALONE]


def test_the_rewrite_is_in_the_trace(alice, central, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search([]))
    events, _ = ask(alice, {"question": "Who is Ren Ashby?"})
    chat_id = dict(events)["answer"]["chat_id"]
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search([], STANDALONE))
    events, _ = ask(alice, {"question": "And next?", "chat_id": chat_id})
    request_id = dict(events)["request"]["request_id"]
    with central() as s:
        trace = s.get(Trace, request_id)
        assert trace.decomposition_json["understood"] == {"standalone": STANDALONE}


def test_someone_elses_chat_is_404_and_reveals_nothing(alice, bob, monkeypatch):
    seen: list = []
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen))
    events, _ = ask(alice, {"question": "Alice's secret question"})
    chat_id = dict(events)["answer"]["chat_id"]
    seen.clear()
    r = bob.post("/api/ask", json={"question": "What did he do next?", "chat_id": chat_id})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert "secret" not in r.text
    assert seen == []  # never searched, so nothing of alice's reached a prompt
    assert len(alice.get(f"/api/chats/{chat_id}").json()["messages"]) == 1


def test_an_unknown_chat_is_404(alice, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search([]))
    r = alice.post("/api/ask", json={"question": "q", "chat_id": "no-such-chat"})
    assert r.status_code == 404


@pytest.mark.parametrize("extra", [
    {"history": [{"question": "Who is X?", "answer": "INJECTED"}]},
    {"turns": [{"question": "Who is X?", "answer": "INJECTED"}]},
    {"standalone": "INJECTED"},
])
def test_a_client_cannot_send_history(alice, monkeypatch, extra):
    seen: list = []
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen))
    r = alice.post("/api/ask", json={"question": "q", **extra})
    assert r.status_code == 400
    assert seen == []


def test_local_mode_follow_ups_work_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    seen: list = []
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen))
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path)),
                        base_url="http://127.0.0.1:8766")
    events, _ = ask(client, {"question": "Who is Ren Ashby?"})
    chat_id = dict(events)["answer"]["chat_id"]
    monkeypatch.setattr("lore.web.app.tools.lore_search", recording_search(seen, STANDALONE))
    events, _ = ask(client, {"question": "And next?", "chat_id": chat_id})
    assert seen[-1]["history"][0]["question"] == "Who is Ren Ashby?"
    msgs = client.get(f"/api/chats/{chat_id}").json()["messages"]
    assert msgs[-1]["standalone"] == STANDALONE


# --- the migration -------------------------------------------------------------------

def test_migration_0008_adds_and_drops_the_standalone_column(tmp_path):
    from alembic import command
    from lore.central.migrate import alembic_config
    url = f"sqlite:///{tmp_path / 'm.db'}"
    central_db.upgrade(url, "0007")
    engine = sa.create_engine(url)
    assert "standalone" not in {c["name"] for c in sa.inspect(engine).get_columns("messages")}
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, role, status, created_at)"
                             " VALUES ('u', 'u@x', 'user', 'active', '2026-01-01')"))
        conn.execute(sa.text("INSERT INTO chats (id, user_id, title, created_at, updated_at)"
                             " VALUES ('c', 'u', 't', '2026-01-01', '2026-01-01')"))
        conn.execute(sa.text("INSERT INTO messages (chat_id, role, question, created_at, size)"
                             " VALUES ('c', 'assistant', 'old q', '2026-01-01', 5)"))
    central_db.upgrade(url)
    engine.dispose()
    engine = sa.create_engine(url)
    assert "standalone" in {c["name"] for c in sa.inspect(engine).get_columns("messages")}
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT question, standalone FROM messages")).one() == ("old q", None)
    engine.dispose()
    command.downgrade(alembic_config(url), "0007")
    engine = sa.create_engine(url)
    assert "standalone" not in {c["name"] for c in sa.inspect(engine).get_columns("messages")}
    engine.dispose()


def test_under_a_reading_position_only_turns_answered_at_that_position_are_used(central):
    note = "Answered from what you've read: up to chapter 3 of B"
    at_position = result("early")
    at_position["position_note"] = note
    later = result("past the position")
    later["position_note"] = "Answered from what you've read: up to chapter 9 of B"
    chat_id = _messages(central, "alice", [
        ("with spoilers", {"result": result("names the culprit"), "steps": []}, None),
        ("at a later position", {"result": later, "steps": []}, None),
        ("at this position", {"result": at_position, "steps": []}, None),
    ])
    with central() as s:
        _, messages = H.get_chat(s, "alice", chat_id)
    assert history_turns(messages, world=None, position_note=note) == [
        {"question": "at this position", "answer": "early"}]
    # With no position on (or spoilers shown), every turn is fair context.
    assert len(history_turns(messages, world=None)) == 3
