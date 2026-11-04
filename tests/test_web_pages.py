"""Multi mode's routes and pages: the public front, the signed-in app, and the
user row that sign-in guarantees. Local mode serves `/` exactly as before."""
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth import accounts
from lore.auth.providers import Profile
from lore.central.models import Job, User
from lore.config import LoreConfig
from lore.tenancy import config_for
from lore.web import app as web_app
from tests.auth_fixtures import (
    BASE_URL, NOW, FakeMailer, FakeProvider, give_key, make_app, sign_in,
)
from tests.central_fixtures import central_sessions
from tests.test_worker import library

INDEX = "<!doctype html><html><head><title>LORE</title></head><body><div id=root></div></body></html>"
MARK = '<meta name="lore-multi" content="1">'


@pytest.fixture
def static(tmp_path, monkeypatch):
    out = tmp_path / "static"
    out.mkdir()
    (out / "index.html").write_text(INDEX)
    monkeypatch.setattr(web_app, "STATIC", out)
    return out


@pytest.fixture
def env(tmp_path, static):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    provider = FakeProvider()
    app = make_app(root, sessions, mailer=FakeMailer(), providers={"google": provider})
    anon = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    return app, sessions, anon, root, provider


def test_the_homepage_is_public_and_marks_the_page_as_multi_user(env):
    _, _, anon, *_ = env
    r = anon.get("/")
    assert r.status_code == 200 and MARK in r.text and "<div id=root>" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path", ["/waitlist", "/privacy", "/terms", "/signin"])
def test_the_public_pages_are_served_to_anyone(env, path):
    _, _, anon, *_ = env
    r = anon.get(path)
    assert r.status_code == 200 and MARK in r.text


@pytest.mark.parametrize("path", ["/app", "/admin"])
def test_the_app_and_admin_need_a_session(env, path):
    app, sessions, anon, *_ = env
    r = anon.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    r = signed.get(path)
    assert r.status_code == 200 and MARK in r.text


def test_an_expired_session_is_sent_to_sign_in(env):
    app, sessions, anon, *_ = env
    anon.cookies.set("lore_session", "not-a-session")
    assert anon.get("/app").headers["location"] == "/signin"


def test_a_signed_in_visitor_to_sign_in_goes_to_the_app(env):
    app, sessions, *_ = env
    signed = sign_in(app, sessions, "alice")
    signed.follow_redirects = False
    r = signed.get("/signin")
    assert r.status_code == 303 and r.headers["location"] == "/app"


def test_the_page_says_so_when_the_app_is_not_built(env, tmp_path, monkeypatch):
    _, _, anon, *_ = env
    monkeypatch.setattr(web_app, "STATIC", tmp_path / "nothing")
    assert "not built yet" in anon.get("/").text
    assert "not built yet" in anon.get("/privacy").text


def test_local_mode_serves_the_app_at_the_root_as_before(tmp_path, static, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    client = TestClient(web_app.create_app(LoreConfig(data_dir=tmp_path / "d")),
                        base_url=BASE_URL, follow_redirects=False)
    r = client.get("/")
    assert r.status_code == 200 and "lore-multi" not in r.text and "<div id=root>" in r.text
    assert r.headers["cache-control"] == "no-cache"
    for path in ("/signin", "/admin", "/app", "/waitlist", "/auth/providers", "/api/me"):
        assert client.get(path).status_code == 404, path


# --- every id the hook returns has a users row ---

def test_a_new_user_exists_centrally_and_can_queue_work(env):
    app, sessions, anon, root, provider = env
    with sessions() as db:
        code = accounts.create_invite(db, "alice", NOW)
        db.commit()
    provider.profile = Profile("g-new", "new@example.org", True, "New")
    r = anon.get(f"/auth/google/start?invite={code}")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    assert anon.get("/auth/google/callback",
                    params={"code": "c", "state": state}).headers["location"] == "/app"
    uid = anon.get("/api/me").json()["user"]["id"]
    with sessions() as db:
        assert db.get(User, uid) is not None
    library(root, uid, "b1")  # their own library, as the worker tests build it
    token = anon.cookies["lore_csrf"]
    r = anon.post("/api/books/b1/continue", headers={"X-CSRF-Token": token})
    assert r.json()["error"]["code"] == "key_required"  # onboarding first
    give_key(sessions, uid)
    r = anon.post("/api/books/b1/continue", headers={"X-CSRF-Token": token})
    assert r.status_code == 202, r.text
    with sessions() as db:
        assert db.scalars(sa.select(Job)).one().user_id == uid


def test_a_session_whose_user_is_gone_reaches_no_library_and_makes_no_directory(env):
    app, sessions, anon, root, _ = env
    signed = sign_in(app, sessions, "alice")
    with sessions() as db:
        db.execute(sa.text("PRAGMA foreign_keys=OFF"))
        db.execute(sa.text("DELETE FROM users WHERE id = 'alice'"))
        db.commit()
    assert signed.get("/api/books").status_code == 401
    assert signed.post("/api/ask", json={"question": "q"}).status_code == 401
    assert not (root.data_dir / "users" / "alice").exists()


def test_signed_out_requests_make_no_directory(env):
    _, _, anon, root, _ = env
    for path in ("/api/books", "/api/library", "/api/overview"):
        assert anon.get(path).status_code == 401
    assert not (root.data_dir / "users").exists()
