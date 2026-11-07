"""The hosted MCP endpoint (/mcp in multi mode): bearer tokens only, each
token reads its own user's library and nothing else, through every tool."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil

import pytest
from fastapi.testclient import TestClient

from lore.central import mcp_tokens as T
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.inference import key_scope
from lore.mcp.hosted import HOSTED_TOOLS, MAX_RESULT_BYTES
from lore.reading_position import ReadingPosition, set_position
from lore.tenancy import config_for
from lore.web.throttle import RateSettings
from tests.auth_fixtures import BASE_URL, NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions
from tests.mcp_fixtures import build_store_triple

HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}
ALICE_KEY = "AIzaALICEaliceALICEaliceALICEaliceALI01"
BOB_KEY = "AIzaBOBbobBOBbobBOBbobBOBbobBOBbobBOB02"


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def libraries(root):
    """alice and bob each own a book, two entities, an edge; carol has none."""
    build_store_triple(
        root.data_dir, books=[("ab", "A Study in Scarlet")],
        chunks=[("ac1", "ab", 1)],
        entities=[("ae1", "Holmes", "Character", ["ab"], []),
                  ("ae2", "Watson", "Character", ["ab"], [])],
        edges=[("ae1", "ae2", "INTERACTS_WITH", "ac1", "ab", 1)],
        jobs=[("extract", "ab", "completed", None, "2026-01-01")],
        data_dir=config_for(root, "alice").data_dir)
    build_store_triple(
        root.data_dir, books=[("bb", "Dracula")],
        chunks=[("bc1", "bb", 1)],
        entities=[("be1", "Count Dracula", "Character", ["bb"], []),
                  ("be2", "Harker", "Character", ["bb"], [])],
        edges=[("be1", "be2", "INTERACTS_WITH", "bc1", "bb", 1)],
        jobs=[("extract", "bb", "completed", None, "2026-01-01")],
        data_dir=config_for(root, "bob").data_dir)


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob", "carol"))
    give_key(sessions, "alice", ALICE_KEY)
    give_key(sessions, "bob", BOB_KEY)
    return sessions


def token_for(central, user: str) -> str:
    with central() as db:
        _, raw = T.create(db, user, f"{user}'s laptop", NOW)
        db.commit()
    return raw


@pytest.fixture
def tokens(central):
    return {u: token_for(central, u) for u in ("alice", "bob", "carol")}


def make(root, central, **kwargs):
    return make_app(root, central, **kwargs)


@pytest.fixture
def client(root, libraries, central):
    with TestClient(make(root, central), base_url=BASE_URL) as c:
        yield c


def rpc(client, token, method, params=None, headers=None):
    h = dict(HEADERS)
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                     "params": params or {}}, headers=h)


def call(client, token, tool, **args) -> dict:
    r = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def code(out: dict) -> str | None:
    return out.get("error", {}).get("code")


# --- authentication -----------------------------------------------------------------

def test_no_token_is_401_with_a_pointer_to_the_resource_metadata(client):
    r = rpc(client, None, "tools/list")
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


@pytest.mark.parametrize("bad", ["lore_nope", "nope", "lore_" + "x" * 43, ""])
def test_an_unknown_token_is_401(client, tokens, bad):
    assert rpc(client, bad, "tools/list").status_code == 401


def test_a_revoked_token_is_401(client, central, tokens):
    assert rpc(client, tokens["alice"], "tools/list").status_code == 200
    with central() as db:
        tid = T.list_for(db, "alice")[0]["id"]
        T.revoke(db, "alice", tid, NOW)
        db.commit()
    assert rpc(client, tokens["alice"], "tools/list").status_code == 401


def test_a_disabled_user_s_token_is_401(client, central, tokens):
    from lore.central.models import User
    with central() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    assert rpc(client, tokens["alice"], "tools/list").status_code == 401


def test_a_session_cookie_alone_does_not_reach_mcp(root, libraries, central):
    app = make(root, central)
    with sign_in(app, central, "alice") as alice:
        r = alice.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                     "params": {}}, headers=HEADERS)
    assert r.status_code == 401


def test_the_protected_resource_metadata_names_this_server(client):
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{BASE_URL}/mcp"
    assert meta["authorization_servers"] == [f"{BASE_URL}/"] or \
        meta["authorization_servers"] == [BASE_URL]


# --- the tools ----------------------------------------------------------------------

def test_the_hosted_tools_are_the_six_read_tools_without_cypher(client, tokens):
    listed = rpc(client, tokens["alice"], "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed} == set(HOSTED_TOOLS)
    assert "cypher_query" not in {t["name"] for t in listed}
    for t in listed:  # nothing that names a user, a path or a store
        props = set(t["inputSchema"].get("properties", {}))
        assert not props & {"user", "user_id", "config", "data_dir", "path", "models", "embed"}


def test_cypher_query_cannot_be_called(client, tokens):
    r = rpc(client, tokens["alice"], "tools/call",
            {"name": "cypher_query", "arguments": {"query": "MATCH (e) RETURN e"}})
    body = r.json()
    assert "error" in body or body["result"].get("isError")
    assert "Holmes" not in r.text


def test_each_token_sees_only_its_own_library(client, tokens):
    assert [b["title"] for b in call(client, tokens["alice"], "library")["books"]] == \
        ["A Study in Scarlet"]
    assert [b["title"] for b in call(client, tokens["bob"], "library")["books"]] == ["Dracula"]
    assert code(call(client, tokens["carol"], "library")) == "no_graph"


def test_no_tool_reaches_another_user_s_library(client, tokens, monkeypatch):
    """alice's token, aimed at everything bob owns, through every tool."""
    async def no_ollama(texts):
        return [[0.0] * 768 for _ in texts]
    from lore.query import agent
    monkeypatch.setattr(agent, "make_query_embedder",
                        lambda cfg: type("E", (), {"embed": staticmethod(no_ollama),
                                                    "aclose": staticmethod(_aclose)})())
    a = tokens["alice"]
    assert code(call(client, a, "entity", name="Count Dracula")) == "not_found"
    assert code(call(client, a, "entity", name="Harker")) == "not_found"
    assert code(call(client, a, "subgraph", entity="Count Dracula")) == "not_found"
    assert code(call(client, a, "ingest_status", book_id="bb")) == "not_found"
    assert code(call(client, a, "vector_search", query="the count",
                     book_id="bb")) == "not_found"
    for ref in ("../bob", "../../users/bob", "bob", "users/bob/lore.db"):
        assert code(call(client, a, "ingest_status", book_id=ref)) == "not_found"
        assert code(call(client, a, "library", world=ref)) == "not_found"
    # ...and the same calls with her own names work.
    assert call(client, a, "entity", name="Holmes")["name"] == "Holmes"
    assert call(client, a, "ingest_status", book_id="ab")["title"] == "A Study in Scarlet"
    assert "Dracula" not in json.dumps(call(client, a, "subgraph", entity="Holmes"))


async def _aclose():
    return None


def test_lore_search_runs_on_the_asker_s_own_key_and_library(client, tokens, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSERVERserverSERVERserverSERVERser9")
    seen = []

    async def fake_search(config, query, on_event=None, world=None, depth=None, **kw):
        from lore.mcp import tools
        scope = key_scope.current()
        seen.append((scope.user_id, scope.key, key_scope.gemini_api_key()))
        titles = [b["title"] for b in tools.library(config)["books"]]
        return {"answer": "I can see: " + ", ".join(titles), "mode": "graph",
                "claims": [], "passages": [], "edges": [], "seeds": [],
                "sub_questions": [query], "model": "gemini-2.5-flash"}
    monkeypatch.setattr("lore.mcp.tools.lore_search", fake_search)
    out = call(client, tokens["alice"], "lore_search", query="who is here?")
    assert out["answer"] == "I can see: A Study in Scarlet"
    assert out["model"] is None  # no model names reach a hosted user
    out = call(client, tokens["bob"], "lore_search", query="who is here?")
    assert out["answer"] == "I can see: Dracula"
    assert seen == [("alice", ALICE_KEY, ALICE_KEY), ("bob", BOB_KEY, BOB_KEY)]
    assert key_scope.current() is None  # nothing stays bound after the call


def test_lore_search_without_a_key_is_key_required(client, tokens, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("must not run without a key")
    monkeypatch.setattr("lore.mcp.tools.lore_search", never)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSERVERserverSERVERserverSERVERser9")
    out = call(client, tokens["carol"], "lore_search", query="anything?")
    assert code(out) == "key_required"


def test_lore_search_shares_the_user_s_question_slot(root, libraries, central, tokens,
                                                     monkeypatch):
    """One question at a time per user, whether it came from the web or MCP."""
    from lore.web.throttle import Throttle
    app = make(root, central)
    with TestClient(app, base_url=BASE_URL) as c:
        throttle = Throttle(lambda: central, RateSettings(), lambda: NOW)
        held = throttle.start_question("alice")  # a question already running
        try:
            out = call(c, tokens["alice"], "lore_search", query="again?")
        finally:
            held.release()
    assert code(out) == "busy"


def test_reading_position_applies_and_the_bypass_lifts_it(root, central, tokens, tmp_path):
    from tests.position_fixtures import HOLLOW, build_position_library
    built = build_position_library(tmp_path / "pos")
    dest = config_for(root, "carol").data_dir
    shutil.rmtree(dest)
    shutil.copytree(built.data_dir, dest)
    conn = open_db(config_for(root, "carol").sqlite_path)
    wid = conn.execute("SELECT id FROM worlds WHERE name = ?", (HOLLOW,)).fetchone()[0]
    set_position(conn, ReadingPosition(wid, "hb1", 2, (), True))
    conn.close()
    with TestClient(make(root, central), base_url=BASE_URL) as c:
        read = call(c, tokens["carol"], "library", world=HOLLOW)
        everything = call(c, tokens["carol"], "library", world=HOLLOW,
                          ignore_reading_position=True)
        hidden = call(c, tokens["carol"], "entity", name="Dorn Vale", world=HOLLOW)
    assert [b["id"] for b in read["books"]] == ["hb1"]
    assert len(everything["books"]) == 3
    assert code(hidden) == "not_found"


# --- limits ---------------------------------------------------------------------------

def test_inputs_are_bounded(client, tokens):
    a = tokens["alice"]
    assert code(call(client, a, "lore_search", query="x" * 2001)) == "bad_request"
    assert code(call(client, a, "lore_search", query="   ")) == "bad_request"
    assert code(call(client, a, "entity", name="x" * 201)) == "bad_request"
    assert code(call(client, a, "library", world="w" * 201)) == "bad_request"


def test_vector_search_returns_at_most_twenty(client, tokens, monkeypatch):
    seen = {}

    async def fake(config, query, book_id=None, limit=10, **kw):
        seen["limit"] = limit
        return {"chunks": []}
    monkeypatch.setattr("lore.mcp.tools.vector_search", fake)
    call(client, tokens["alice"], "vector_search", query="q", limit=50)
    assert seen["limit"] == 20


def test_an_oversized_result_is_refused_not_sent(client, tokens, monkeypatch):
    big = {"books": [{"title": "x" * 1000} for _ in range(MAX_RESULT_BYTES // 900)]}
    monkeypatch.setattr("lore.mcp.tools.library", lambda config, **kw: big)
    out = call(client, tokens["alice"], "library")
    assert code(out) == "too_large"


def test_calls_are_rate_limited_per_token(root, libraries, central, tokens):
    app = make(root, central, rate_settings=RateSettings(mcp_per_minute=2))
    with TestClient(app, base_url=BASE_URL) as c:
        outs = [call(c, tokens["alice"], "library") for _ in range(3)]
        other = call(c, tokens["bob"], "library")  # bob's token has its own count
    assert [code(o) for o in outs] == [None, None, "rate_limited"]
    assert outs[2]["error"]["retry_after"] >= 1
    assert code(other) is None


def test_calls_are_rate_limited_per_user_across_tokens(root, libraries, central):
    second = token_for(central, "alice")
    first = token_for(central, "alice")
    app = make(root, central, rate_settings=RateSettings(mcp_per_minute=5,
                                                         mcp_per_minute_per_user=3))
    with TestClient(app, base_url=BASE_URL) as c:
        outs = [call(c, t, "library") for t in (first, second, first, second)]
    assert [code(o) for o in outs] == [None, None, None, "rate_limited"]


def test_each_call_is_logged_by_token_without_book_text(client, tokens, caplog):
    caplog.set_level(logging.INFO, logger="lore.mcp.hosted")
    call(client, tokens["alice"], "entity", name="Holmes")
    call(client, tokens["alice"], "library")
    lines = [r.getMessage() for r in caplog.records if r.name == "lore.mcp.hosted"]
    assert len(lines) == 2
    assert all("pat:" in line and "alice" in line for line in lines)
    text = "\n".join(lines)
    for secret in ("Holmes", "Watson", "Study in Scarlet", tokens["alice"],
                   "INTERACTS_WITH"):
        assert secret not in text


# --- origins and hosts --------------------------------------------------------------

def test_a_browser_page_on_another_origin_is_refused(client, tokens):
    r = rpc(client, tokens["alice"], "tools/list", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_an_mcp_client_without_an_origin_is_served(client, tokens):
    assert rpc(client, tokens["alice"], "tools/list").status_code == 200
    r = rpc(client, tokens["alice"], "tools/list", headers={"Origin": BASE_URL})
    assert r.status_code == 200


def test_a_foreign_host_is_refused(client, tokens):
    r = rpc(client, tokens["alice"], "tools/list", headers={"Host": "evil.example"})
    assert r.status_code == 403


# --- the settings endpoints ---------------------------------------------------------

def test_api_mcp_gives_the_hosted_address(root, libraries, central):
    app = make(root, central)
    alice = sign_in(app, central, "alice")
    body = alice.get("/api/mcp").json()
    assert body["hosted"] is True and body["mcp_url"] == f"{BASE_URL}/mcp"
    assert body["max_tokens"] == T.MAX_TOKENS_PER_USER
    check = alice.get("/api/mcp/check").json()
    assert check["ok"] is True and {t["name"] for t in check["tools"]} == set(HOSTED_TOOLS)


def test_local_mode_has_no_hosted_endpoint(tmp_path):
    from lore.web.app import create_app
    app = create_app(LoreConfig(data_dir=tmp_path / "d"))
    with TestClient(app, base_url="http://127.0.0.1:8766") as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={**HEADERS, "Authorization": "Bearer lore_x"})
    assert r.status_code in (404, 405)


def test_the_concurrent_calls_of_two_users_never_cross(root, libraries, central, tokens):
    """Many interleaved calls from both users: each sees only its own books."""
    app = make(root, central, rate_settings=RateSettings(mcp_per_minute=0,
                                                         mcp_per_minute_per_user=0))
    import concurrent.futures
    with TestClient(app, base_url=BASE_URL) as c:
        def one(user):
            return user, [b["title"] for b in call(c, tokens[user], "library")["books"]]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(one, ["alice", "bob"] * 10))
    for user, titles in results:
        assert titles == (["A Study in Scarlet"] if user == "alice" else ["Dracula"])
