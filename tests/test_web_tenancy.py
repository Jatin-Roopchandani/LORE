"""Multi-user mode: every endpoint serves the requesting user's own library
and nothing of anyone else's. A fake search stands in for the model."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from lore.config import LoreConfig
from lore.tenancy import CurrentUser, config_for
from lore.web.app import create_app
from tests.auth_fixtures import (
    ACT_AS as HEADER, ActAsClient, give_key, make_app, make_settings,
)
from tests.central_fixtures import central_sessions
from tests.mcp_fixtures import build_store_triple

# Every request here carries a REAL session (cookie, CSRF token and a row in
# the central database): `ActAsClient` turns `headers={HEADER: "alice"}` into
# alice's session, so the test still goes through the cookie -> user hook.


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def libraries(root):
    """alice and bob each own a book, two entities, an edge and a world-less
    library; carol has no library at all."""
    build_store_triple(
        root.data_dir, books=[("ab", "A Study in Scarlet")],
        chunks=[("ac1", "ab", 1)],
        entities=[("ae1", "Holmes", "Character", ["ab"], []),
                  ("ae2", "Watson", "Character", ["ab"], [])],
        edges=[("ae1", "ae2", "INTERACTS_WITH", "ac1", "ab", 1)],
        data_dir=config_for(root, "alice").data_dir)
    build_store_triple(
        root.data_dir, books=[("bb", "Dracula")],
        chunks=[("bc1", "bb", 1)],
        entities=[("be1", "Dracula", "Character", ["bb"], []),
                  ("be2", "Harker", "Character", ["bb"], [])],
        edges=[("be1", "be2", "INTERACTS_WITH", "bc1", "bb", 1)],
        data_dir=config_for(root, "bob").data_dir)


async def library_search(config, query, models=None, on_event=None, world=None,
                         depth=None):
    """A fake model: 'answers' with the titles of the books it can see."""
    from lore.mcp import tools
    on_event({"type": "parts", "sub_questions": [query]})
    await asyncio.sleep(0)
    titles = [b["title"] for b in tools.library(config)["books"]]
    return {"answer": "I can see: " + ", ".join(titles), "mode": "graph",
            "claims": [], "passages": [], "edges": [], "seeds": [],
            "sub_questions": [query], "data_dir": str(config.data_dir)}


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob", "carol"))
    for user in ("alice", "bob", "carol"):  # asking needs the user's own key
        give_key(sessions, user)
    return sessions


@pytest.fixture
def client(root, libraries, central, monkeypatch):
    monkeypatch.setattr("lore.web.app.tools.lore_search", library_search)
    return ActAsClient(make_app(root, central), central)


def get(client, user, path, **params):
    r = client.get(path, params=params, headers={HEADER: user})
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


def ask(client, user, question="who?"):
    with client.stream("POST", "/api/ask", json={"question": question},
                       headers={HEADER: user}) as r:
        body = b"".join(r.iter_bytes()).decode()
    events = {}
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events[lines["event"]] = json.loads(lines["data"])
    return events


# Each read endpoint: (path, params for alice's data, params for bob's data).
READS = [
    ("/api/library", {}, {}),
    ("/api/books", {}, {}),
    ("/api/worlds", {}, {}),
    ("/api/overview", {}, {}),
    ("/api/entity", {"id": "ae1"}, {"id": "be1"}),
    ("/api/entity", {"name": "Holmes"}, {"name": "Dracula"}),
    ("/api/entities", {"ids": "ae1,ae2"}, {"ids": "be1,be2"}),
    ("/api/subgraph", {"id": "ae1"}, {"id": "be1"}),
    ("/api/entity/ae1/profile", {}, {}),
    ("/api/entity/ae1/timeline", {}, {}),
    ("/api/entity/ae1/relations", {}, {}),
    ("/api/edge", {"a": "ae1", "b": "ae2"}, {"a": "be1", "b": "be2"}),
    ("/api/search", {"q": "Holmes"}, {"q": "Dracula"}),
    ("/api/path", {"a": "ae1", "b": "ae2"}, {"a": "be1", "b": "be2"}),
    ("/api/passage/ac1", {}, {}),
    ("/api/quota", {}, {}),
]
ALICE_MARKERS = ("Scarlet", "Holmes", "Watson", "ae1", "ae2", "ac1", "\"ab\"")
BOB_MARKERS = ("Dracula", "Harker", "be1", "be2", "bc1", "\"bb\"")


def test_each_user_sees_their_own_library(client):
    assert [b["title"] for b in get(client, "alice", "/api/library")["books"]] == [
        "A Study in Scarlet"]
    assert [b["title"] for b in get(client, "bob", "/api/library")["books"]] == ["Dracula"]
    assert get(client, "alice", "/api/entity", id="ae1")["name"] == "Holmes"
    assert get(client, "bob", "/api/entity", id="be1")["name"] == "Dracula"
    assert [b["id"] for b in get(client, "alice", "/api/books")["books"]] == ["ab"]
    assert [b["id"] for b in get(client, "bob", "/api/books")["books"]] == ["bb"]


def _leaks(body, markers, *request_parts):
    """The markers found in `body`, except any the request itself carried
    (an error that echoes the id asked for leaks nothing)."""
    asked = json.dumps(request_parts)
    text = json.dumps(body)
    return [m for m in markers if m in text and m not in asked]


@pytest.mark.parametrize("path,a_params,b_params", READS)
def test_no_read_endpoint_shows_one_user_the_others_data(client, path, a_params,
                                                         b_params):
    # Bob asks for Alice's ids and names: nothing of hers comes back.
    assert _leaks(get(client, "bob", path, **a_params), ALICE_MARKERS,
                  path, a_params) == []
    # ...and the reverse.
    assert _leaks(get(client, "alice", path, **b_params), BOB_MARKERS,
                  path, b_params) == []


@pytest.mark.parametrize("path,a_params", [
    (p, a) for p, a, _ in READS if p not in ("/api/quota", "/api/worlds")])
def test_alice_does_see_her_own_data_on_each_endpoint(client, path, a_params):
    body = get(client, "alice", path, **a_params)
    assert not (isinstance(body, dict) and "error" in body), body


def test_alice_and_bob_are_told_apart_by_the_same_query(client):
    # The same entity id exists in neither library under the other's name.
    assert get(client, "alice", "/api/entity", id="be1")["error"]["code"] == "not_found"
    assert get(client, "bob", "/api/entity", id="ae1")["error"]["code"] == "not_found"
    assert get(client, "bob", "/api/entity", name="Holmes")["error"]["code"] == "not_found"


def test_a_user_without_a_library_sees_an_empty_one(client):
    assert get(client, "carol", "/api/books")["books"] == []
    library = get(client, "carol", "/api/library")
    assert library.get("books") == [] or library["error"]["code"] == "no_graph"


def test_a_worlds_edits_stay_in_the_users_library(client):
    r = client.post("/api/worlds", json={"name": "Baker Street"},
                    headers={HEADER: "alice"})
    assert r.status_code == 201
    assert [w["name"] for w in get(client, "alice", "/api/worlds")] == ["Baker Street"]
    assert get(client, "bob", "/api/worlds") == []


def test_ask_runs_against_the_askers_library_only(client, root):
    a = ask(client, "alice")["answer"]["result"]
    b = ask(client, "bob")["answer"]["result"]
    assert a["answer"] == "I can see: A Study in Scarlet"
    assert b["answer"] == "I can see: Dracula"
    assert a["data_dir"] == str(root.data_dir / "users" / "alice")
    assert b["data_dir"] == str(root.data_dir / "users" / "bob")


def test_one_users_running_question_does_not_block_another(root, libraries, central,
                                                           monkeypatch):
    gate = asyncio.Event()
    started = []

    async def slow(config, query, models=None, on_event=None, **_):
        started.append(config.data_dir.name)
        await gate.wait()
        return {"answer": "x", "mode": "graph", "claims": [], "passages": [],
                "edges": [], "seeds": [], "sub_questions": []}

    monkeypatch.setattr("lore.web.app.tools.lore_search", slow)
    app = make_app(root, central)
    from tests.auth_fixtures import sign_in
    creds = {}
    for user in ("alice", "bob"):
        c = sign_in(app, central, user)
        creds[user] = {"Cookie": "; ".join(f"{k}={v}" for k, v in c.cookies.items()),
                       "X-CSRF-Token": c.headers["X-CSRF-Token"]}

    async def go():
        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://127.0.0.1:8766") as c:
            async def post(user):
                r = await c.post("/api/ask", json={"question": "q"},
                                 headers=creds[user])
                return r.text
            first = asyncio.create_task(post("alice"))
            while started != ["alice"]:
                await asyncio.sleep(0.01)
            again = asyncio.create_task(post("alice"))   # alice is busy with herself
            other = asyncio.create_task(post("bob"))     # bob is not
            while "bob" not in started:
                await asyncio.sleep(0.01)
            gate.set()
            return await first, await again, await other

    first, again, other = asyncio.run(go())
    assert "event: answer" in first and "event: answer" in other
    assert "busy" in again


def test_unauthenticated_requests_are_refused_everywhere(client):
    for path, _, _ in READS:
        assert client.get(path).status_code == 401, path
    assert client.post("/api/ask", json={"question": "q"}).status_code == 401
    assert client.post("/api/worlds", json={"name": "w"}).status_code == 401
    assert client.post("/api/books/ab/continue").status_code == 401


@pytest.mark.parametrize("evil", ["..", "../alice", "alice/../bob", "a/b", "/etc",
                                  "ALICE", "local", ""])
def test_path_traversal_ids_are_rejected_and_touch_nothing(client, root, evil):
    # Even a user row with such an id, holding a live session, reaches no path.
    from lore.central.models import User
    if evil:
        with client._sessions() as db:
            db.add(User(id=evil, email=f"{abs(hash(evil))}@example.org"))
            db.commit()
    before = sorted(p.name for p in (root.data_dir / "users").iterdir())
    r = client.get("/api/library", headers={HEADER: evil})
    assert r.status_code == 401
    r = client.post("/api/ask", json={"question": "q"}, headers={HEADER: evil})
    assert r.status_code == 401
    assert sorted(p.name for p in (root.data_dir / "users").iterdir()) == before
    assert sorted(p.name for p in root.data_dir.iterdir()) == ["users"]


def test_a_resolver_cannot_hand_out_the_local_library_in_multi_mode(root, libraries,
                                                                    central):
    app = make_app(root, central, resolve_user=lambda r: CurrentUser(
        "local", is_local=True))
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    assert client.get("/api/library").status_code == 401


def test_the_default_multi_mode_resolver_fails_closed(root, libraries, monkeypatch):
    # The whole production path: flag and secrets from the environment, no
    # session cookie, so no library. (The header means nothing to the app.)
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    monkeypatch.setenv("LORE_SESSION_SECRET", "s" * 40)
    monkeypatch.setenv("LORE_CSRF_SECRET", "c" * 40)
    monkeypatch.setenv("LORE_BASE_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("LORE_COOKIE_SECURE", "0")
    monkeypatch.setenv("LORE_KEY_ENCRYPTION_KEY", "a" * 43 + "=")  # 32 bytes, base64
    client = TestClient(create_app(root), base_url="http://127.0.0.1:8766")
    assert client.get("/api/library", headers={HEADER: "alice"}).status_code == 401


def test_local_mode_ignores_any_user_and_serves_the_base_library(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    config = build_store_triple(tmp_path, books=[("b1", "A Study")])
    client = TestClient(create_app(config), base_url="http://127.0.0.1:8766")
    plain = client.get("/api/library").json()
    spoofed = client.get("/api/library", headers={HEADER: "alice"}).json()
    assert plain == spoofed and plain["books"][0]["title"] == "A Study"
    assert not (config.data_dir / "users").exists()
    assert not (tmp_path / "users").exists()
