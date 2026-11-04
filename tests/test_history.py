"""Server-side history (release step 4): chats and messages per user.

The central functions (lore.central.history) and the API (lore.web.history),
in multi mode through real sessions, and in local mode with no sign-in.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central import history as H
from lore.central.models import Chat, Message, User
from lore.config import LoreConfig
from lore.web.app import create_app
from tests.auth_fixtures import NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

RESULT = {"answer": "Holmes met Watson.", "mode": "graph", "claims": [], "passages": [],
          "edges": [], "seeds": [], "sub_questions": ["who?"], "answer_style": "claims"}


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


# --- lore.central.history -------------------------------------------------------

def test_append_without_a_chat_makes_one_titled_from_the_question(central):
    with central() as s:
        chat, msg = H.append_message(s, "alice", None, question="  Who   is Holmes?\n",
                                     answer={"result": RESULT, "steps": []}, depth="auto",
                                     world="1", request_id="r1", now=NOW)
        s.commit()
        assert chat.title == "Who is Holmes?" and chat.user_id == "alice"
        assert msg.chat_id == chat.id and msg.answer_json["result"] == RESULT
        chat2, msg2 = H.append_message(s, "alice", chat.id, question="And Watson?",
                                       answer=None, depth=None, world="1", request_id=None,
                                       now=NOW + timedelta(minutes=1))
        s.commit()
        assert chat2.id == chat.id and chat2.updated_at == NOW + timedelta(minutes=1)
        _, messages = H.get_chat(s, "alice", chat.id)
        assert [m.question for m in messages] == ["  Who   is Holmes?\n", "And Watson?"]


def test_long_titles_are_cut():
    assert H.title_from("x" * 300).endswith("…") and len(H.title_from("x" * 300)) == H.TITLE_CHARS
    assert H.title_from("   ") == "Untitled"


def test_another_users_chat_is_not_found_for_every_operation(central):
    with central() as s:
        chat, _ = H.create_chat(s, "alice", title="mine", now=NOW)
        s.commit()
        for op in (lambda: H.get_chat(s, "bob", chat.id),
                   lambda: H.rename_chat(s, "bob", chat.id, "x"),
                   lambda: H.delete_chat(s, "bob", chat.id),
                   lambda: H.append_message(s, "bob", chat.id, question="q", answer=None,
                                            depth=None, world=None, request_id=None)):
            with pytest.raises(H.NotFound):
                op()
        assert s.get(Chat, chat.id).title == "mine"


def test_soft_delete_hides_the_chat_but_keeps_the_row(central):
    with central() as s:
        chat, _ = H.create_chat(s, "alice", title="gone", now=NOW)
        H.delete_chat(s, "alice", chat.id, NOW)
        s.commit()
        with pytest.raises(H.NotFound):
            H.get_chat(s, "alice", chat.id)
        assert H.list_chats(s, "alice")[0] == []
        assert s.get(Chat, chat.id).deleted_at == NOW


def test_listing_is_newest_first_and_paginated(central):
    with central() as s:
        for i in range(5):
            H.append_message(s, "alice", None, question=f"q{i}", answer={"result": RESULT},
                             depth=None, world=None, request_id=None,
                             now=NOW + timedelta(minutes=i))
        H.append_message(s, "bob", None, question="bob's", answer=None, depth=None,
                         world=None, request_id=None, now=NOW)
        s.commit()
        page, nxt = H.list_chats(s, "alice", limit=2)
        assert [c.title for c, _ in page] == ["q4", "q3"] and nxt == 2
        page, nxt = H.list_chats(s, "alice", limit=2, offset=4)
        assert [c.title for c, _ in page] == ["q0"] and nxt is None
        assert page[0][1].question == "q0"  # the latest message comes with it


def test_at_most_max_chats_soft_deleted_go_first(central, monkeypatch):
    monkeypatch.setattr(H, "MAX_CHATS", 3)
    with central() as s:
        ids = []
        for i in range(3):
            chat, _ = H.create_chat(s, "alice", title=f"c{i}", now=NOW + timedelta(minutes=i))
            ids.append(chat.id)
        H.delete_chat(s, "alice", ids[2], NOW)  # the newest, but deleted
        s.commit()
        H.create_chat(s, "alice", title="c3", now=NOW + timedelta(minutes=9))
        s.commit()
        left = s.scalars(sa.select(Chat.title).where(Chat.user_id == "alice")).all()
        assert sorted(left) == ["c0", "c1", "c3"]
        H.create_chat(s, "alice", title="c4", now=NOW + timedelta(minutes=10))
        s.commit()  # nothing deleted left: the least recently used goes
        left = s.scalars(sa.select(Chat.title).where(Chat.user_id == "alice")).all()
        assert sorted(left) == ["c1", "c3", "c4"]
        assert s.scalar(sa.select(sa.func.count()).select_from(Message)) == 0


def test_an_answer_over_one_megabyte_is_stored_as_a_marker(central):
    huge = {"result": {**RESULT, "answer": "x" * (H.MAX_ANSWER_BYTES + 10)}}
    with central() as s:
        _, msg = H.append_message(s, "alice", None, question="q", answer=huge, depth=None,
                                  world=None, request_id=None, now=NOW)
        s.commit()
        assert msg.answer_json["truncated"] is True and msg.answer_json["bytes"] > H.MAX_ANSWER_BYTES


def test_import_by_client_id_is_idempotent(central):
    with central() as s:
        first, created = H.create_chat(s, "alice", client_id="17000", messages=[
            {"question": "Who?", "answer": {"result": RESULT, "steps": []}}], now=NOW)
        again, created2 = H.create_chat(s, "alice", client_id="17000", messages=[
            {"question": "Who?", "answer": {"result": RESULT, "steps": []}}], now=NOW)
        bobs, created3 = H.create_chat(s, "bob", client_id="17000", now=NOW)
        s.commit()
        assert created and not created2 and created3
        assert first.id == again.id != bobs.id
        assert first.title == "Who?"
        assert len(H.get_chat(s, "alice", first.id)[1]) == 1


def test_deleting_the_user_deletes_their_chats_and_messages(central):
    with central() as s:
        H.append_message(s, "alice", None, question="q", answer=None, depth=None,
                         world=None, request_id=None, now=NOW)
        s.commit()
        s.execute(sa.delete(User).where(User.id == "alice"))
        s.commit()
        assert s.scalar(sa.select(sa.func.count()).select_from(Chat)) == 0
        assert s.scalar(sa.select(sa.func.count()).select_from(Message)) == 0


# --- the API, multi mode --------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def app(root, central):
    return make_app(root, central)


@pytest.fixture
def alice(app, central):
    return sign_in(app, central, "alice")


@pytest.fixture
def bob(app, central):
    return sign_in(app, central, "bob")


def test_crud_through_the_api(alice):
    r = alice.post("/api/chats", json={"title": "Holmes"})
    assert r.status_code == 201 and r.json()["created"] is True
    chat_id = r.json()["chat"]["id"]
    assert alice.patch(f"/api/chats/{chat_id}", json={"title": "Sherlock"}).json()["chat"]["title"] == "Sherlock"
    listed = alice.get("/api/chats").json()
    assert [c["title"] for c in listed["chats"]] == ["Sherlock"] and listed["next_offset"] is None
    got = alice.get(f"/api/chats/{chat_id}").json()
    assert got["chat"]["id"] == chat_id and got["messages"] == []
    assert alice.delete(f"/api/chats/{chat_id}").json() == {"deleted": True}
    assert alice.get(f"/api/chats/{chat_id}").status_code == 404
    assert alice.get("/api/chats").json()["chats"] == []


def test_another_users_chat_is_404_not_403(alice, bob):
    chat_id = alice.post("/api/chats", json={"title": "private"}).json()["chat"]["id"]
    assert bob.get(f"/api/chats/{chat_id}").status_code == 404
    assert bob.patch(f"/api/chats/{chat_id}", json={"title": "x"}).status_code == 404
    assert bob.delete(f"/api/chats/{chat_id}").status_code == 404
    assert bob.get(f"/api/chats/no-such-id").status_code == 404
    assert bob.get("/api/chats").json()["chats"] == []
    assert alice.get(f"/api/chats/{chat_id}").json()["chat"]["title"] == "private"


def test_the_routes_need_a_session_and_mutations_need_csrf(app, alice):
    anon = TestClient(app, base_url=alice.base_url)
    assert anon.get("/api/chats").status_code == 401
    del alice.headers["X-CSRF-Token"]
    assert alice.post("/api/chats", json={"title": "x"}).status_code == 403


def test_pagination_through_the_api(alice):
    for i in range(3):
        alice.post("/api/chats", json={"title": f"t{i}"})
    page = alice.get("/api/chats?limit=2").json()
    assert len(page["chats"]) == 2 and page["next_offset"] == 2
    assert len(alice.get("/api/chats?limit=2&offset=2").json()["chats"]) == 1
    assert alice.get("/api/chats?limit=0").status_code == 422


def test_import_is_idempotent_and_never_links_a_trace(alice):
    body = {"client_id": "1730000000000", "title": "Who is Holmes?", "world": "all",
            "created_at": "2026-10-01T10:00:00+00:00",
            "messages": [{"question": "Who is Holmes?", "world": "all", "depth": "auto",
                          "answer": {"result": RESULT, "steps": [{"type": "parts", "t": 0.1,
                                                                  "sub_questions": ["who?"]}]},
                          "request_id": "forged"}]}
    first = alice.post("/api/chats", json=body)
    again = alice.post("/api/chats", json=body)
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["chat"]["id"] == again.json()["chat"]["id"]
    assert first.json()["chat"]["created_at"].startswith("2026-10-01")
    (msg,) = alice.get(f"/api/chats/{first.json()['chat']['id']}").json()["messages"]
    assert msg["request_id"] is None and msg["answer"]["result"] == RESULT
    assert len(alice.get("/api/chats").json()["chats"]) == 1


def test_a_future_import_time_is_not_trusted(alice):
    r = alice.post("/api/chats", json={"client_id": "c", "created_at": "2999-01-01T00:00:00+00:00",
                                       "messages": [{"question": "q"}]})
    assert not r.json()["chat"]["created_at"].startswith("2999")


def _fake_search(answer=RESULT):
    async def search(config, query, models=None, on_event=None, **_):
        on_event({"type": "step", "name": "split", "state": "start", "part": None, "detail": None})
        on_event({"type": "parts", "sub_questions": [query]})
        await asyncio.sleep(0)
        return answer
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


def test_ask_appends_to_a_new_chat_then_to_the_given_one(alice, central, monkeypatch):
    give_key(central, "alice")
    monkeypatch.setattr("lore.web.app.tools.lore_search", _fake_search())
    events, _ = ask(alice, {"question": "Who is Holmes?", "world": "all", "depth": "deep"})
    answer = dict(events)["answer"]
    chat_id = answer["chat_id"]
    assert answer["result"] == RESULT
    chats = alice.get("/api/chats").json()["chats"]
    assert [c["id"] for c in chats] == [chat_id] and chats[0]["title"] == "Who is Holmes?"
    assert chats[0]["latest"]["summary"]["deep"] is False
    events, _ = ask(alice, {"question": "And Watson?", "chat_id": chat_id})
    assert dict(events)["answer"]["chat_id"] == chat_id
    msgs = alice.get(f"/api/chats/{chat_id}").json()["messages"]
    assert [m["question"] for m in msgs] == ["Who is Holmes?", "And Watson?"]
    assert msgs[0]["depth"] == "deep" and msgs[0]["world"] == "all"
    steps = msgs[0]["answer"]["steps"]
    assert [s["type"] for s in steps] == ["step", "parts"] and all("t" in s for s in steps)


def test_ask_with_someone_elses_chat_id_starts_a_new_chat(alice, bob, central, monkeypatch):
    give_key(central, "bob")
    chat_id = alice.post("/api/chats", json={"title": "alice's"}).json()["chat"]["id"]
    monkeypatch.setattr("lore.web.app.tools.lore_search", _fake_search())
    events, _ = ask(bob, {"question": "q", "chat_id": chat_id})
    assert dict(events)["answer"]["chat_id"] != chat_id
    assert alice.get(f"/api/chats/{chat_id}").json()["messages"] == []


def test_a_failed_answer_adds_no_message(alice, central, monkeypatch):
    give_key(central, "alice")
    monkeypatch.setattr("lore.web.app.tools.lore_search",
                        _fake_search({"error": {"code": "budget_exhausted", "message": "spent"}}))
    events, _ = ask(alice, {"question": "q"})
    assert "error" in dict(events)
    assert alice.get("/api/chats").json()["chats"] == []


# --- local mode: no sign-in, the same code on a local central database ----------

def test_local_mode_history_works_without_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.setattr("lore.web.app.tools.lore_search", _fake_search())
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path)), base_url="http://127.0.0.1:8766")
    events, _ = ask(client, {"question": "Who is Holmes?"})
    chat_id = dict(events)["answer"]["chat_id"]
    assert [c["id"] for c in client.get("/api/chats").json()["chats"]] == [chat_id]
    assert (tmp_path / "central.db").exists()
    assert client.delete(f"/api/chats/{chat_id}").json() == {"deleted": True}
