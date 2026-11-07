"""The Settings popup's server side: signed-in devices (list, revoke one,
sign out of the others), the linked sign-in methods on /api/me, Download my
data, and the MCP tab's setup and Check (local mode only).

Each mutating route is checked for its CSRF token, each route for a session,
and every one for per-user isolation: alice's requests never see or touch
bob's sessions, chats or books."""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from lore.auth import devices
from lore.auth import sessions as auth_sessions
from lore.auth.tokens import hash_token
from lore.central import history as H
from lore.central.models import AuthSession, Identity, User
from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.tenancy import config_for
from lore.web.app import create_app
from tests.auth_fixtures import BASE_URL, FAKE_KEY, NOW, give_key, make_app, sign_in
from tests.central_fixtures import central_sessions

ALICE = str(uuid.UUID(int=11))
BOB = str(uuid.UUID(int=12))
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=())
    with sessions() as db:
        db.add(User(id=ALICE, email="alice@example.org"))
        db.add(User(id=BOB, email="bob@example.org"))
        db.commit()
    return sessions


@pytest.fixture
def root(tmp_path):
    return LoreConfig(data_dir=tmp_path / "root")


@pytest.fixture
def app(root, central):
    return make_app(root, central)


def other_session(central, uid, *, ua=None, at=NOW) -> str:
    """A session of `uid`'s on another device; returns its token."""
    with central() as db:
        token = auth_sessions.create_session(db, uid, at, user_agent=ua)
        db.commit()
    return token


def alive(central, token) -> bool:
    with central() as db:
        return db.get(AuthSession, hash_token(token)) is not None


# --- the device label ---------------------------------------------------------------

@pytest.mark.parametrize("ua, label", [
    (FIREFOX, "Firefox on Linux"),
    (SAFARI, "Safari on iPhone"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/130.0 Safari/537.36 Edg/130.0", "Edge on Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/130.0 Safari/537.36", "Chrome on macOS"),
    (None, "Unknown browser"),
    ("curl/8.0", "Unknown browser"),
])
def test_device_label(ua, label):
    assert devices.device_label(ua) == label


# --- GET /api/me/sessions -----------------------------------------------------------

def test_sessions_lists_mine_with_this_device_marked(app, central):
    phone = other_session(central, ALICE, ua=SAFARI, at=NOW - timedelta(hours=3))
    other_session(central, BOB, ua=FIREFOX)
    alice = sign_in(app, central, ALICE)
    r = alice.get("/api/me/sessions")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    listed = r.json()["sessions"]
    assert len(listed) == 2  # bob's session is not among them
    current = [s for s in listed if s["current"]]
    assert len(current) == 1
    assert current[0]["id"] == hash_token(alice.session_token)[:16]
    other = next(s for s in listed if not s["current"])
    assert other["device"] == "Safari on iPhone"
    assert other["id"] == hash_token(phone)[:16]
    assert set(other) == {"id", "device", "created_at", "last_active", "current"}
    # Nothing that signs anyone in: no token, no full hash.
    assert phone not in r.text and hash_token(phone) not in r.text


def test_sessions_leaves_out_expired_ones(app, central):
    old = other_session(central, ALICE, at=NOW - timedelta(days=8))  # idle past 7 days
    alice = sign_in(app, central, ALICE)
    ids = [s["id"] for s in alice.get("/api/me/sessions").json()["sessions"]]
    assert hash_token(old)[:16] not in ids and len(ids) == 1


def test_sessions_needs_a_session(app):
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.get("/api/me/sessions").status_code == 401
    assert anon.delete("/api/me/sessions/0123456789abcdef").status_code == 401
    assert anon.post("/api/me/sessions/sign-out-others").status_code == 401


# --- DELETE /api/me/sessions/{id} ---------------------------------------------------

def test_revoking_another_of_my_devices_signs_it_out(app, central):
    phone = other_session(central, ALICE, ua=SAFARI)
    alice = sign_in(app, central, ALICE)
    r = alice.delete(f"/api/me/sessions/{hash_token(phone)[:16]}")
    assert r.status_code == 200 and r.json() == {"revoked": 1}
    assert not alive(central, phone)
    assert alice.get("/api/me").status_code == 200  # this device stays signed in
    gone = TestClient(app, base_url=BASE_URL)
    gone.cookies.set("lore_session", phone)
    assert gone.get("/api/me").status_code == 401


def test_revoking_needs_the_csrf_token(app, central):
    phone = other_session(central, ALICE)
    alice = sign_in(app, central, ALICE)
    del alice.headers["X-CSRF-Token"]
    assert alice.delete(f"/api/me/sessions/{hash_token(phone)[:16]}").status_code == 403
    alice.headers["X-CSRF-Token"] = "0" * 64
    assert alice.delete(f"/api/me/sessions/{hash_token(phone)[:16]}").status_code == 403
    assert alive(central, phone)


def test_revoking_someone_elses_session_is_not_found_and_leaves_it(app, central):
    bobs = other_session(central, BOB)
    alice = sign_in(app, central, ALICE)
    r = alice.delete(f"/api/me/sessions/{hash_token(bobs)[:16]}")
    assert r.status_code == 404
    assert alive(central, bobs)


def test_revoking_this_device_says_to_sign_out_instead(app, central):
    alice = sign_in(app, central, ALICE)
    r = alice.delete(f"/api/me/sessions/{hash_token(alice.session_token)[:16]}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "current_session"
    assert alive(central, alice.session_token)


@pytest.mark.parametrize("bad", ["abc", "ZZZZZZZZZZZZZZZZ", "0123456789abcdef0", "%25"])
def test_revoking_a_malformed_id_is_not_found(app, central, bad):
    other_session(central, ALICE)
    alice = sign_in(app, central, ALICE)
    assert alice.delete(f"/api/me/sessions/{bad}").status_code == 404
    assert len(alice.get("/api/me/sessions").json()["sessions"]) == 2


# --- POST /api/me/sessions/sign-out-others ------------------------------------------

def test_sign_out_of_other_devices_keeps_this_one_and_bobs(app, central):
    a1, a2 = other_session(central, ALICE, ua=SAFARI), other_session(central, ALICE)
    bobs = other_session(central, BOB)
    alice = sign_in(app, central, ALICE)
    r = alice.post("/api/me/sessions/sign-out-others")
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    assert not alive(central, a1) and not alive(central, a2)
    assert alive(central, alice.session_token) and alive(central, bobs)
    assert [s["current"] for s in alice.get("/api/me/sessions").json()["sessions"]] == [True]


def test_sign_out_of_other_devices_needs_the_csrf_token(app, central):
    a1 = other_session(central, ALICE)
    alice = sign_in(app, central, ALICE)
    del alice.headers["X-CSRF-Token"]
    assert alice.post("/api/me/sessions/sign-out-others").status_code == 403
    assert alive(central, a1)


# --- /api/me: the linked sign-in methods -------------------------------------------

def test_me_lists_my_sign_in_methods_only(app, central):
    with central() as db:
        db.add(Identity(provider="email", provider_user_id="alice@example.org", user_id=ALICE,
                        created_at=NOW))
        db.add(Identity(provider="github", provider_user_id="77", user_id=ALICE,
                        created_at=NOW + timedelta(days=1)))
        db.add(Identity(provider="google", provider_user_id="99", user_id=BOB, created_at=NOW))
        db.commit()
    body = sign_in(app, central, ALICE).get("/api/me").json()
    assert body["user"]["email"] == "alice@example.org"
    assert [i["provider"] for i in body["identities"]] == ["email", "github"]
    assert "provider_user_id" not in json.dumps(body) and "77" not in json.dumps(body["identities"])


# --- POST /api/me/export ------------------------------------------------------------

def library_with_book(root, uid, title):
    conn = open_db(config_for(root, uid).sqlite_path)
    try:
        conn.execute("INSERT INTO books (id, title, author, file_path) VALUES (?, ?, ?, ?)",
                     (f"book-{title}", title, "An Author", f"/srv/secret/{title}.epub"))
        conn.commit()
    finally:
        conn.close()


def ask(central, uid, question):
    with central() as db:
        H.append_message(db, uid, None, question=question,
                         answer={"result": {"answer_style": "prose", "prose": [{"text": "yes"}]}},
                         depth="auto", world=None, request_id=None, now=NOW)
        db.commit()


def unzip(r) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return {name: zf.read(name).decode() for name in zf.namelist()}


def test_export_is_my_chats_and_books_and_nothing_of_bobs(app, central, root):
    give_key(central, ALICE)
    ask(central, ALICE, "Who is Holmes?")
    ask(central, BOB, "Bob's private question")
    library_with_book(root, ALICE, "Alices Book")
    library_with_book(root, BOB, "Bobs Book")
    r = sign_in(app, central, ALICE).post("/api/me/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith('attachment; filename="lore-data-')
    assert r.headers["cache-control"] == "no-store"
    files = unzip(r)
    assert set(files) == {"README.txt", "chats.json", "books.json"}
    chats = json.loads(files["chats.json"])["chats"]
    assert [c["messages"][0]["question"] for c in chats] == ["Who is Holmes?"]
    assert chats[0]["messages"][0]["answer"]["result"]["prose"] == [{"text": "yes"}]
    books = json.loads(files["books.json"])
    assert [b["title"] for b in books["books"]] == ["Alices Book"]
    everything = "".join(files.values())
    assert "Bob" not in everything
    assert FAKE_KEY not in everything and FAKE_KEY[-4:] not in everything
    assert "/srv/secret" not in everything  # no server paths


def test_export_of_an_empty_account(app, central):
    files = unzip(sign_in(app, central, BOB).post("/api/me/export"))
    assert json.loads(files["chats.json"]) == {"chats": []}
    assert json.loads(files["books.json"]) == {"books": [], "worlds": []}


def test_export_needs_a_session_and_the_csrf_token(app, central):
    assert TestClient(app, base_url=BASE_URL).post("/api/me/export").status_code == 401
    alice = sign_in(app, central, ALICE)
    del alice.headers["X-CSRF-Token"]
    assert alice.post("/api/me/export").status_code == 403
    assert alice.get("/api/me/export").status_code in (404, 405)  # never a GET


def test_export_in_local_mode(tmp_path):
    cfg = LoreConfig(data_dir=tmp_path / "lib")
    client = TestClient(create_app(cfg), base_url=BASE_URL)
    files = unzip(client.post("/api/me/export"))
    assert set(files) == {"README.txt", "chats.json", "books.json"}


# --- the MCP tab: setup snippets and Check (local mode only) ------------------------

MCP_TOOLS = {"lore_search", "vector_search", "cypher_query", "entity", "subgraph", "library",
             "ingest_status"}


@pytest.fixture
def local(tmp_path, monkeypatch):
    from lore.web import app as app_module
    monkeypatch.chdir(tmp_path)  # no lore.toml here
    exe = tmp_path / "bin" / "lore"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(app_module, "_lore_executable", lambda: str(exe))
    cfg = LoreConfig(data_dir=tmp_path / "my lib")
    return TestClient(create_app(cfg), base_url=BASE_URL), cfg, exe


def test_mcp_setup_snippets_use_the_real_command_and_library(local):
    client, cfg, exe = local
    body = client.get("/api/mcp").json()
    data_dir = str(cfg.data_dir.resolve())
    desktop = json.loads(body["claude_desktop"])
    assert desktop == {"mcpServers": {"lore": {"command": str(exe), "args": ["mcp"],
                                               "env": {"LORE_DATA_DIR": data_dir}}}}
    assert body["claude_code"] == (f"claude mcp add --scope user lore "
                                   f"-e 'LORE_DATA_DIR={data_dir}' -- {exe} mcp")
    assert body["http_command"] == f"LORE_DATA_DIR='{data_dir}' {exe} mcp --http --port 8765"
    assert body["claude_code_http"].endswith("--transport http lore http://127.0.0.1:8765/mcp")
    assert body["library"]["data_dir"] == data_dir


def test_mcp_setup_pins_lore_toml_when_the_server_has_one(local, tmp_path):
    client, _, _ = local
    (tmp_path / "lore.toml").write_text("[lore]\n")
    env = json.loads(client.get("/api/mcp").json()["claude_desktop"])["mcpServers"]["lore"]["env"]
    assert env["LORE_CONFIG"] == str((tmp_path / "lore.toml").resolve())


def test_mcp_check_builds_the_server_and_lists_its_tools(local):
    client, _, _ = local
    body = client.get("/api/mcp/check").json()
    assert body["ok"] is True and body["lore_found"] is True and body["problem"] is None
    assert {t["name"] for t in body["tools"]} == MCP_TOOLS
    assert all(t["summary"] and "\n" not in t["summary"] for t in body["tools"])


def test_mcp_check_says_when_the_lore_command_is_missing(local, monkeypatch):
    from lore.web import app as app_module
    client, _, _ = local
    monkeypatch.setattr(app_module, "_lore_executable", lambda: "/nowhere/lore")
    body = client.get("/api/mcp/check").json()
    assert body["ok"] is False and "/nowhere/lore" in body["problem"]


def test_mcp_check_reports_a_server_that_cannot_start(local, monkeypatch):
    client, _, _ = local

    def broken(config):
        raise RuntimeError("no")
    monkeypatch.setattr("lore.mcp.server.build_server", broken)
    body = client.get("/api/mcp/check").json()
    assert body["ok"] is False and "RuntimeError" in body["problem"]


def test_mcp_routes_are_not_there_in_multi_mode(app, central):
    alice = sign_in(app, central, ALICE)
    assert alice.get("/api/mcp").status_code == 404
    assert alice.get("/api/mcp/check").status_code == 404


# --- LORE_CONFIG ------------------------------------------------------------------

def test_lore_config_names_the_toml_file(tmp_path, monkeypatch):
    toml = tmp_path / "elsewhere.toml"
    toml.write_text('[lore]\ndata_dir = "from-toml"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LORE_DATA_DIR", raising=False)
    monkeypatch.setenv("LORE_CONFIG", str(toml))
    assert str(LoreConfig.load().data_dir) == "from-toml"
    monkeypatch.delenv("LORE_CONFIG")
    assert str(LoreConfig.load().data_dir) == "data"
