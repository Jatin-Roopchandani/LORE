"""Personal access tokens for the hosted MCP endpoint: storage and the
/api/me/mcp-tokens API (session-auth only, CSRF-protected, capped)."""
from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth.tokens import hash_token
from lore.central import mcp_tokens as T
from lore.central.models import McpToken
from lore.config import LoreConfig
from tests.auth_fixtures import BASE_URL, NOW, make_app, sign_in
from tests.central_fixtures import central_sessions


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def app(tmp_path, central):
    return make_app(LoreConfig(data_dir=tmp_path / "root"), central)


# --- storage ------------------------------------------------------------------------

def test_a_token_is_shown_once_and_only_its_hash_is_stored(central):
    with central() as db:
        row, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
    assert raw.startswith("lore_") and len(raw) > 40
    with central() as db:
        stored = db.get(McpToken, row.id)
        assert stored.token_hash == hash_token(raw)
        assert raw not in {stored.token_hash, stored.prefix, stored.name}
        assert raw.startswith(stored.prefix) and len(stored.prefix) < 16
        # Nowhere in the row: no column holds the token itself.
        values = [str(v) for v in db.execute(sa.select(McpToken.__table__)).one()]
        assert all(raw not in v for v in values)
    assert raw not in repr(stored)


def test_resolve_finds_the_owner_and_stamps_last_used(central):
    with central() as db:
        row, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
    later = NOW + timedelta(minutes=5)
    with central() as db:
        found = T.resolve(db, raw, later)
        db.commit()
    assert found is not None and found.user_id == "alice" and found.id == row.id
    with central() as db:
        assert db.get(McpToken, row.id).last_used_at == later


@pytest.mark.parametrize("raw", [None, "", "lore_", "lore_nope", "x" * 500,
                                 "Bearer lore_x", "lore_" + "a" * 300])
def test_resolve_refuses_anything_unknown(central, raw):
    with central() as db:
        T.create(db, "alice", "laptop", NOW)
        db.commit()
        assert T.resolve(db, raw, NOW) is None


def test_a_revoked_token_no_longer_resolves(central):
    with central() as db:
        row, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
        assert T.revoke(db, "alice", row.id, NOW) is True
        db.commit()
        assert T.resolve(db, raw, NOW) is None
        assert T.revoke(db, "alice", row.id, NOW) is False  # already gone


def test_a_disabled_user_s_token_does_not_resolve(central):
    from lore.central.models import User
    with central() as db:
        _, raw = T.create(db, "alice", "laptop", NOW)
        db.get(User, "alice").status = "disabled"
        db.commit()
        assert T.resolve(db, raw, NOW) is None


def test_one_user_cannot_revoke_another_s_token(central):
    with central() as db:
        row, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
        assert T.revoke(db, "bob", row.id, NOW) is False
        db.commit()
        assert T.resolve(db, raw, NOW) is not None


def test_tokens_per_user_are_capped(central):
    with central() as db:
        for i in range(T.MAX_TOKENS_PER_USER):
            T.create(db, "alice", f"t{i}", NOW)
        db.commit()
        with pytest.raises(T.TooManyTokens):
            T.create(db, "alice", "one more", NOW)
        T.create(db, "bob", "bob's", NOW)  # another user's count is their own
        first = T.list_for(db, "alice")[0]
        T.revoke(db, "alice", first["id"], NOW)
        T.create(db, "alice", "fits again", NOW)


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, "tab\there", "nl\nhere", 7, None])
def test_a_bad_name_is_refused(central, name):
    with central() as db, pytest.raises(T.BadName):
        T.create(db, "alice", name, NOW)


def test_the_list_never_carries_the_token_or_its_hash(central):
    with central() as db:
        row, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
        listed = T.list_for(db, "alice")
    assert listed == [{"id": row.id, "name": "laptop", "prefix": row.prefix,
                       "created_at": NOW.isoformat(), "last_used_at": None}]


# --- the API ------------------------------------------------------------------------

def test_create_list_and_revoke_through_the_api(app, central):
    alice = sign_in(app, central, "alice")
    made = alice.post("/api/me/mcp-tokens", json={"name": "Claude Code"})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["token"].startswith("lore_") and body["name"] == "Claude Code"
    assert made.headers["cache-control"] == "no-store"
    listed = alice.get("/api/me/mcp-tokens").json()
    assert [t["id"] for t in listed["tokens"]] == [body["id"]]
    assert "token" not in listed["tokens"][0]
    assert listed["max_tokens"] == T.MAX_TOKENS_PER_USER
    assert alice.delete(f"/api/me/mcp-tokens/{body['id']}").json() == {"revoked": True}
    assert alice.get("/api/me/mcp-tokens").json()["tokens"] == []


def test_the_api_is_csrf_protected(app, central):
    tid = sign_in(app, central, "alice").post(
        "/api/me/mcp-tokens", json={"name": "x"}).json()["id"]
    no_csrf = sign_in(app, central, "alice")
    del no_csrf.headers["X-CSRF-Token"]
    assert no_csrf.post("/api/me/mcp-tokens", json={"name": "y"}).status_code == 403
    assert no_csrf.delete(f"/api/me/mcp-tokens/{tid}").status_code == 403
    assert len(no_csrf.get("/api/me/mcp-tokens").json()["tokens"]) == 1


def test_a_token_cannot_mint_tokens(app, central):
    alice = sign_in(app, central, "alice")
    raw = alice.post("/api/me/mcp-tokens", json={"name": "x"}).json()["token"]
    bearer = TestClient(app, base_url=BASE_URL)
    auth = {"Authorization": f"Bearer {raw}"}
    assert bearer.post("/api/me/mcp-tokens", json={"name": "y"}, headers=auth).status_code == 401
    assert bearer.get("/api/me/mcp-tokens", headers=auth).status_code == 401
    assert bearer.delete("/api/me/mcp-tokens/whatever", headers=auth).status_code == 401


def test_another_user_s_token_is_not_found(app, central):
    alice = sign_in(app, central, "alice")
    tid = alice.post("/api/me/mcp-tokens", json={"name": "x"}).json()["id"]
    bob = sign_in(app, central, "bob")
    assert bob.delete(f"/api/me/mcp-tokens/{tid}").status_code == 404
    assert bob.get("/api/me/mcp-tokens").json()["tokens"] == []
    assert len(alice.get("/api/me/mcp-tokens").json()["tokens"]) == 1


def test_the_api_caps_and_validates(app, central):
    alice = sign_in(app, central, "alice")
    assert alice.post("/api/me/mcp-tokens", json={"name": ""}).status_code == 400
    assert alice.post("/api/me/mcp-tokens", json={}).status_code == 400
    for i in range(T.MAX_TOKENS_PER_USER):
        assert alice.post("/api/me/mcp-tokens", json={"name": f"t{i}"}).status_code == 201
    over = alice.post("/api/me/mcp-tokens", json={"name": "more"})
    assert over.status_code == 409 and over.json()["error"]["code"] == "too_many_tokens"


def test_creating_tokens_is_rate_limited(app, central):
    alice = sign_in(app, central, "alice")
    codes = []
    for i in range(T.CREATES_PER_HOUR + 1):
        r = alice.post("/api/me/mcp-tokens", json={"name": f"t{i}"})
        if r.status_code == 201:
            alice.delete(f"/api/me/mcp-tokens/{r.json()['id']}")
        codes.append(r.status_code)
    assert codes[-1] == 429 and set(codes[:-1]) == {201}


def test_local_mode_has_no_token_api(tmp_path):
    from lore.web.app import create_app
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path / "d")), base_url=BASE_URL)
    assert client.get("/api/me/mcp-tokens").status_code in (404, 405)


def test_account_deletion_removes_the_tokens(app, central, tmp_path):
    from lore.central.erase import erase_user
    with central() as db:
        _, raw = T.create(db, "alice", "laptop", NOW)
        db.commit()
    erase_user(central, tmp_path / "root", "alice", now=NOW)
    with central() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(McpToken)) == 0
        assert T.resolve(db, raw, NOW) is None
