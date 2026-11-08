"""Resource limits on the history (security review, release step 4): request
bodies are capped by Content-Length and by bytes actually streamed, each
stored answer is capped by its serialized size, and each user has a total
stored-bytes quota."""
from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from lore.central import history as H
from lore.central.models import Chat, Message
from lore.config import LoreConfig
from lore.web import limits
from tests.auth_fixtures import NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

RESULT = {"answer": "x", "mode": "graph", "claims": [], "passages": [], "edges": [],
          "seeds": [], "sub_questions": [], "answer_style": "claims"}


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def app(tmp_path, central):
    return make_app(LoreConfig(data_dir=tmp_path / "root"), central)


@pytest.fixture
def alice(app, central):
    return sign_in(app, central, "alice")


# --- request bodies -------------------------------------------------------------

def test_an_import_over_the_limit_is_413_by_content_length(alice):
    body = json.dumps({"title": "t", "messages": [{"question": "q", "answer": {
        "result": {"answer": "x" * (limits.IMPORT_BYTES + 10)}}}]})
    r = alice.post("/api/chats", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert alice.get("/api/chats").json()["chats"] == []


def test_a_streamed_body_without_content_length_is_counted(alice):
    def chunks():
        yield b'{"title": "t", "messages": [{"question": "q", "answer": {"x": "'
        for _ in range(40):
            yield b"y" * 100_000
        yield b'"}}]}'
    r = alice.post("/api/chats", content=chunks(), headers={"Content-Type": "application/json"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413
    assert alice.get("/api/chats").json()["chats"] == []


def test_a_lying_content_length_is_still_counted():
    """The byte count, not the header, is what decides."""
    seen = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            seen.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []
    messages = [{"type": "http.request", "body": b"x" * 200_000, "more_body": True},
                {"type": "http.request", "body": b"x" * 200_000, "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/api/worlds",
             "headers": [(b"content-length", b"10")]}
    asyncio.run(limits.BodyLimit(app)(scope, receive, send))
    assert sent[0]["status"] == 413 and seen == []


def test_other_api_routes_have_a_smaller_limit(alice):
    r = alice.post("/api/worlds", json={"name": "x" * (limits.DEFAULT_BYTES + 10)})
    assert r.status_code == 413
    assert alice.post("/api/worlds", json={"name": "Elsewhere"}).status_code != 413


def test_the_ask_route_is_capped_too(alice):
    r = alice.post("/api/ask", json={"question": "x" * (limits.DEFAULT_BYTES + 10)})
    assert r.status_code == 413


def test_a_normal_import_passes(alice):
    r = alice.post("/api/chats", json={"client_id": "c1", "messages": [
        {"question": "q", "answer": {"result": RESULT, "steps": []}}]})
    assert r.status_code == 201


# --- each stored answer ---------------------------------------------------------

def test_an_imported_answer_over_one_megabyte_is_stored_as_the_marker(alice, monkeypatch):
    monkeypatch.setattr(H, "MAX_ANSWER_BYTES", 1000)
    r = alice.post("/api/chats", json={"client_id": "c1", "messages": [
        {"question": "q", "answer": {"result": {**RESULT, "answer": "z" * 5000}, "steps": []}}]})
    (msg,) = alice.get(f"/api/chats/{r.json()['chat']['id']}").json()["messages"]
    assert msg["answer"] == {"truncated": True, "bytes": msg["answer"]["bytes"]}
    assert msg["answer"]["bytes"] > 5000


# --- the per-user quota ---------------------------------------------------------

def _total(s, user):
    return s.scalar(sa.select(sa.func.coalesce(sa.func.sum(Message.size), 0))
                    .join(Chat, Chat.id == Message.chat_id).where(Chat.user_id == user))


def test_every_message_records_its_stored_size(central):
    with central() as s:
        _, msg = H.append_message(s, "alice", None, question="q", answer={"result": RESULT},
                                  depth=None, world=None, request_id=None, now=NOW)
        assert msg.size == len("q") + len(json.dumps({"result": RESULT}, ensure_ascii=False))


def test_soft_deleted_chats_are_evicted_to_make_room(central, monkeypatch):
    big = {"result": {"answer": "a" * 4000}}
    monkeypatch.setattr(H, "MAX_USER_BYTES", 10_000)
    with central() as s:
        c1, _ = H.append_message(s, "alice", None, question="1", answer=big, depth=None,
                                 world=None, request_id=None, now=NOW)
        c2, _ = H.append_message(s, "alice", None, question="2", answer=big, depth=None,
                                 world=None, request_id=None, now=NOW)
        H.delete_chat(s, "alice", c1.id, NOW)
        s.commit()
        H.append_message(s, "alice", None, question="3", answer=big, depth=None,
                         world=None, request_id=None, now=NOW)
        s.commit()
        assert s.get(Chat, c1.id) is None and s.get(Chat, c2.id) is not None
        assert _total(s, "alice") <= 10_000


def test_a_full_history_is_refused_with_a_clear_error(central, monkeypatch):
    big = {"result": {"answer": "a" * 4000}}
    monkeypatch.setattr(H, "MAX_USER_BYTES", 10_000)
    with central() as s:
        for q in ("1", "2"):
            H.append_message(s, "alice", None, question=q, answer=big, depth=None,
                             world=None, request_id=None, now=NOW)
        s.commit()
        with pytest.raises(H.HistoryFull):
            H.append_message(s, "alice", None, question="3", answer=big, depth=None,
                             world=None, request_id=None, now=NOW)
        s.rollback()
        with pytest.raises(H.HistoryFull):
            H.create_chat(s, "alice", client_id="x", messages=[{"question": "q", "answer": big}],
                          now=NOW)
        s.rollback()
        # bob's quota is his own
        H.append_message(s, "bob", None, question="b", answer=big, depth=None, world=None,
                         request_id=None, now=NOW)


def test_the_api_says_history_full(alice, monkeypatch):
    monkeypatch.setattr(H, "MAX_USER_BYTES", 500)
    r = alice.post("/api/chats", json={"client_id": "c1", "messages": [
        {"question": "q", "answer": {"result": {**RESULT, "answer": "z" * 2000}}}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "history_full"


def test_an_answer_that_does_not_fit_is_still_shown_but_not_saved(alice, central, monkeypatch):
    give_key(central, "alice")
    monkeypatch.setattr(H, "MAX_USER_BYTES", 50)

    async def search(config, query, on_event=None, **_):
        return {**RESULT, "answer": "w" * 500}
    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    with alice.stream("POST", "/api/ask", json={"question": "q"}) as r:
        text = b"".join(r.iter_bytes()).decode()
    answer = json.loads(text.split("event: answer\ndata: ")[1].split("\n")[0])
    assert answer["result"]["answer"] == "w" * 500
    assert answer.get("history_full") is True and "chat_id" not in answer
    assert alice.get("/api/chats").json()["chats"] == []
