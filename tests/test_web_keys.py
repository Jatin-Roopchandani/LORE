"""The key API and the key gate on asking and processing (release step 3, task 2).

GET/PUT/DELETE /api/me/key, GET /api/me/usage; a user with no key gets
`key_required` from ask and Continue; a key Gemini refuses mid-question gets
`key_invalid`, is marked, and pauses that user's queued jobs. The key check is
a fake (no network)."""
import json

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central import keys as K
from lore.central import queue
from lore.central.models import ApiKey, Job, Usage
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference.gemini import GeminiClient, GeminiError
from lore.inference.key_scope import gemini_api_key
from lore.tenancy import config_for
from tests.auth_fixtures import (
    BASE_URL, FAKE_KEY, NOW, FakeKeyChecker, give_key, make_app, make_settings,
    master_keys, sign_in,
)
from tests.central_fixtures import central_sessions

OTHER_KEY = "AIzaOTHERotherOTHERotherOTHERother45678"


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def checker():
    return FakeKeyChecker()


@pytest.fixture
def app(root, central, checker):
    return make_app(root, central, key_checker=checker)


@pytest.fixture
def alice(app, central):
    return sign_in(app, central, "alice")


@pytest.fixture
def bob(app, central):
    return sign_in(app, central, "bob")


def put(client, key=FAKE_KEY):
    return client.put("/api/me/key", json={"key": key})


# --- the key API ----------------------------------------------------------------

def test_no_key_yet(alice):
    r = alice.get("/api/me/key")
    assert r.status_code == 200
    assert r.json() == {"present": False, "last4": None, "added_at": None,
                        "last_ok_at": None, "last_error": None}
    assert r.headers["cache-control"] == "no-store"


def test_save_then_read_shows_only_the_last_four(alice, checker):
    r = put(alice)
    assert r.status_code == 200, r.text
    assert checker.seen == [FAKE_KEY]
    body = r.json()
    assert body["present"] and body["last4"] == FAKE_KEY[-4:] and body["check"] == "ok"
    assert body["last_ok_at"] and body["last_error"] is None
    assert FAKE_KEY not in r.text
    got = alice.get("/api/me/key")
    assert got.json()["last4"] == FAKE_KEY[-4:] and FAKE_KEY not in got.text


def test_an_invalid_key_is_refused_with_a_friendly_message(alice, checker):
    checker.result = K.CHECK_INVALID
    r = put(alice)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "key_invalid" and "Gemini" in err["message"]
    assert FAKE_KEY not in r.text
    assert alice.get("/api/me/key").json()["present"] is False


def test_a_key_that_could_not_be_checked_is_stored_with_a_warning(alice, checker):
    checker.result = K.CHECK_UNAVAILABLE
    r = put(alice)
    assert r.status_code == 200
    body = r.json()
    assert body["present"] and body["check"] == "unavailable" and body["warning"]
    assert body["last_error"] == "unchecked" and body["last_ok_at"] is None


@pytest.mark.parametrize("payload", [{"key": "short"}, {"key": ""}, {"key": 12345},
                                     {"nope": FAKE_KEY}, ["x"]])
def test_a_malformed_request_is_400_and_never_echoes_the_input(alice, checker, payload):
    r = alice.put("/api/me/key", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "key_format"
    assert checker.seen == []
    assert "12345" not in r.text and FAKE_KEY not in r.text and "short" not in r.text


def test_a_non_json_body_is_400_and_not_echoed(alice):
    r = alice.put("/api/me/key", content=f"key={FAKE_KEY}",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400 and FAKE_KEY not in r.text


def test_delete_removes_the_key(alice):
    put(alice)
    r = alice.delete("/api/me/key")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert alice.get("/api/me/key").json()["present"] is False
    assert alice.delete("/api/me/key").json() == {"deleted": False}


def test_the_routes_need_a_session(app):
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.get("/api/me/key").status_code == 401
    assert anon.put("/api/me/key", json={"key": FAKE_KEY}).status_code == 401
    assert anon.delete("/api/me/key").status_code == 401
    assert anon.get("/api/me/usage").status_code == 401


@pytest.mark.parametrize("method", ["put", "delete"])
def test_changing_the_key_needs_the_csrf_token(alice, method):
    del alice.headers["X-CSRF-Token"]
    kwargs = {"json": {"key": FAKE_KEY}} if method == "put" else {}
    r = getattr(alice, method)("/api/me/key", **kwargs)
    assert r.status_code == 403
    alice.headers["X-CSRF-Token"] = "forged"
    assert getattr(alice, method)("/api/me/key", **kwargs).status_code == 403


def test_saving_is_limited_to_ten_a_user_an_hour(alice, bob, checker):
    for _ in range(10):
        assert put(alice).status_code == 200
    r = put(alice)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert len(checker.seen) == 10
    assert put(bob).status_code == 200  # per user


def test_users_keys_do_not_mix(alice, bob, central):
    put(alice, FAKE_KEY)
    put(bob, OTHER_KEY)
    assert alice.get("/api/me/key").json()["last4"] == FAKE_KEY[-4:]
    assert bob.get("/api/me/key").json()["last4"] == OTHER_KEY[-4:]
    with central() as db:
        rows = {r.user_id: r for r in db.scalars(sa.select(ApiKey))}
    assert set(rows) == {"alice", "bob"}


def test_local_mode_has_no_key_api(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    from lore.web.app import create_app
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path)), base_url=BASE_URL)
    assert client.get("/api/me/key").status_code == 404
    assert client.get("/api/me/usage").status_code == 404


def test_multi_mode_refuses_to_start_without_the_master_key(root, central, monkeypatch):
    from lore.auth.settings import AuthConfigError
    from lore.web.app import create_app
    monkeypatch.delenv(K.ENV_MASTER, raising=False)
    with pytest.raises(AuthConfigError) as err:
        create_app(root, multi_user=True, central_sessions=central, auth=make_settings())
    assert K.ENV_MASTER in str(err.value)


def test_multi_mode_reads_the_master_key_from_the_environment(root, central, monkeypatch):
    import base64
    from lore.web.app import create_app
    monkeypatch.setenv(K.ENV_MASTER, base64.b64encode(b"k" * 32).decode())
    create_app(root, multi_user=True, central_sessions=central, auth=make_settings())


# --- the key gate on asking and processing -----------------------------------------

def ask(client, question="who?"):
    with client.stream("POST", "/api/ask", json={"question": question}) as r:
        body = b"".join(r.iter_bytes()).decode()
    events = {}
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events[lines["event"]] = json.loads(lines["data"])
    return events, body


def test_asking_without_a_key_is_key_required(alice, monkeypatch):
    called = []

    async def search(config, query, **_):
        called.append(query)
        return {"answer": "x"}

    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    events, _ = ask(alice)
    assert events["error"]["error"]["code"] == "key_required"
    assert called == []


def test_continue_without_a_key_is_key_required(alice, root):
    _add_book(root, "alice", "b1")
    r = alice.post("/api/books/b1/continue")
    assert r.status_code == 409 and r.json()["error"]["code"] == "key_required"


def test_move_without_a_key_is_key_required(alice, root):
    _add_book(root, "alice", "b1")
    r = alice.post("/api/books/b1/move", json={"world": "Elsewhere", "create": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "key_required"


def test_continue_with_a_key_queues(alice, root, central):
    _add_book(root, "alice", "b1")
    give_key(central, "alice")
    assert alice.post("/api/books/b1/continue").status_code == 202


def _add_book(root, user, book_id):
    from tests.test_worker import library
    library(root, user, book_id)


def _gemini_answering(status: int):
    """A fake search that builds a real GeminiClient through the key factory and
    sends one request, answered by `status` (no network)."""
    seen = []

    async def search(config, query, on_event=None, **_):
        def reply(request):
            seen.append(request.headers["x-goog-api-key"])
            if status == 200:
                return httpx.Response(200, json={"candidates": [
                    {"content": {"parts": [{"text": "ok"}]}}]})
            return httpx.Response(status, json={"error": {"message": "no"}})

        client = GeminiClient(gemini_api_key(), models=["gemini-3.1-flash-lite"],
                              transport=httpx.MockTransport(reply))
        try:
            text = await client.generate("q")
        except GeminiError:
            return {"error": {"code": "upstream_unavailable",
                              "message": "Gemini rejected the API key"}}
        finally:
            await client.aclose()
        return {"answer": text, "mode": "graph", "claims": [], "passages": [],
                "edges": [], "seeds": [], "sub_questions": [query]}
    return search, seen


def test_each_user_asks_with_their_own_key(alice, bob, central, monkeypatch):
    give_key(central, "alice", FAKE_KEY)
    give_key(central, "bob", OTHER_KEY)
    search, seen = _gemini_answering(200)
    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    assert "answer" in ask(alice)[0]
    assert "answer" in ask(bob)[0]
    assert ask(alice)[0]["answer"]["result"]["answer"] == "ok"
    assert seen == [FAKE_KEY, OTHER_KEY, FAKE_KEY]


def test_asks_are_counted_per_user_in_usage(alice, bob, central, monkeypatch):
    give_key(central, "alice", FAKE_KEY)
    give_key(central, "bob", OTHER_KEY)
    search, _ = _gemini_answering(200)
    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    ask(alice), ask(alice), ask(bob)
    with central() as db:
        rows = {(u.user_id, u.model): u.requests for u in db.scalars(sa.select(Usage))}
    assert rows == {("alice", "gemini-3.1-flash-lite"): 2,
                    ("bob", "gemini-3.1-flash-lite"): 1}
    r = alice.get("/api/me/usage")
    assert r.status_code == 200
    assert r.json()["requests"] == 2 and r.json()["limit"] == K.DAILY_ALLOWANCE
    assert bob.get("/api/me/usage").json()["requests"] == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_key_invalid_and_pauses_only_that_users_jobs(
        alice, central, monkeypatch, status):
    give_key(central, "alice", FAKE_KEY)
    give_key(central, "bob", OTHER_KEY)
    with central() as db:
        queue.enqueue(db, "alice", "a-book", "process")
        queue.enqueue(db, "bob", "b-book", "process")
    search, _ = _gemini_answering(status)
    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    events, body = ask(alice)
    err = events["error"]["error"]
    assert err["code"] == "key_invalid"
    assert "Traceback" not in body and "gemini.env" not in body and FAKE_KEY not in body
    assert alice.get("/api/me/key").json()["last_error"] == "rejected"
    with central() as db:
        states = {j.user_id: j.state for j in db.scalars(sa.select(Job))}
    assert states == {"alice": "paused", "bob": "queued"}


def test_saving_a_new_key_resumes_the_paused_jobs(alice, central, monkeypatch):
    give_key(central, "alice", FAKE_KEY)
    with central() as db:
        queue.enqueue(db, "alice", "a-book", "process")
    search, _ = _gemini_answering(401)
    monkeypatch.setattr("lore.web.app.tools.lore_search", search)
    ask(alice)
    assert put(alice, OTHER_KEY).status_code == 200
    assert alice.get("/api/me/key").json()["last_error"] is None
    with central() as db:
        assert db.scalar(sa.select(Job.state)) == "queued"


def test_the_admin_sees_who_has_a_key_and_todays_requests(app, central, monkeypatch):
    from lore.central.models import User
    with central() as db:
        db.get(User, "alice").role = "admin"
        db.commit()
    give_key(central, "bob", OTHER_KEY)
    K.KeyService(central, master_keys(), FakeKeyChecker(),
                 clock=lambda: NOW).record_request("bob", "m")
    admin = sign_in(app, central, "alice")
    users = {u["id"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert users["bob"]["has_key"] is True and users["bob"]["requests_today"] == 1
    assert users["alice"]["has_key"] is False and users["alice"]["requests_today"] == 0


# --- the entity intro (the one explorer read that may call a model) --------------

def test_the_entity_intro_runs_on_the_readers_own_key(alice, central, monkeypatch):
    give_key(central, "alice")
    seen = []

    def intro(config, entity_id, **_):
        seen.append((config.data_dir.name, gemini_api_key()))
        return {"intro": "x", "source": "model"}

    monkeypatch.setattr("lore.web.app.explore.intro", intro)
    r = alice.get("/api/entity/e1/intro")
    assert r.status_code == 200 and r.json()["source"] == "model"
    assert seen == [("alice", FAKE_KEY)]


def test_the_entity_intro_without_a_key_never_reaches_the_server_key(alice, monkeypatch):
    from lore.inference.key_scope import KeyRequired
    seen = []

    def intro(config, entity_id, **_):
        try:
            gemini_api_key(local=lambda: "SERVER-KEY")
        except KeyRequired:
            seen.append("required")
        return {"intro": None, "source": "fallback"}

    monkeypatch.setattr("lore.web.app.explore.intro", intro)
    assert alice.get("/api/entity/e1/intro").status_code == 200
    assert seen == ["required"]


def test_intro_model_takes_its_key_from_the_bound_scope(tmp_path, monkeypatch):
    from lore.inference import key_scope
    from lore.query import explore
    from lore.config import SelectionConfig
    cfg = LoreConfig(data_dir=tmp_path, selection=SelectionConfig(provider="gemini"))
    monkeypatch.setattr("lore.inference.gemini.resolve_api_key",
                        lambda **_: pytest.fail("the server key was read"))
    keys = []
    monkeypatch.setattr("lore.inference.gemini.GeminiClient.__init__",
                        lambda self, key, **kw: keys.append(key) or setattr(self, "last_model", None))
    with key_scope.bound(key_scope.KeyScope("alice", OTHER_KEY)):
        made = explore._intro_model(cfg)
    assert made is not None and keys == [OTHER_KEY]
