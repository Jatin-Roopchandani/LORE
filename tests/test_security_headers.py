"""Security headers and session lifetimes (release step 6, task 3)."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth import sessions as auth_sessions
from lore.central.models import AuthSession
from lore.config import LoreConfig
from lore.web.headers import CSP
from tests.auth_fixtures import BASE_URL, NOW, Clock, make_app, make_settings, sign_in
from tests.central_fixtures import central_sessions

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture
def env(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    clock = Clock()
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions, auth_clock=clock)
    return app, sessions, clock


def assert_secured(r, hsts: bool = False) -> None:
    h = r.headers
    assert h["content-security-policy"] == CSP
    assert h["x-content-type-options"] == "nosniff"
    assert h["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert "referrer-policy" in h
    assert ("strict-transport-security" in h) is hsts


def test_the_policy_itself():
    parts = dict(p.split(" ", 1) for p in CSP.split("; "))
    assert parts["default-src"] == "'self'"
    assert parts["script-src"] == "'self'"  # no 'unsafe-inline', no hashes
    assert parts["style-src"] == "'self'"
    assert parts["img-src"] == "'self' data:"
    assert parts["frame-ancestors"] == "'none'"
    assert "unsafe" not in CSP


def test_headers_on_html_api_static_and_refusals(env):
    app, sessions, _ = env
    anon = TestClient(app, base_url=BASE_URL)
    page = anon.get("/signin")
    assert page.headers["content-type"].startswith("text/html")
    assert_secured(page)
    assert page.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert page.headers["x-frame-options"] == "DENY"
    api = sign_in(app, sessions, "alice").get("/api/me")
    assert api.status_code == 200
    assert_secured(api)
    assert_secured(anon.get("/favicon.svg"))           # a static file
    assert_secured(anon.get("/api/books"))             # 401
    assert_secured(anon.get("/api/books", headers={"Origin": "https://evil.example"}))  # 403
    assert_secured(anon.get("/no-such-file.js"))       # 404


def test_a_routes_own_stricter_referrer_policy_is_kept(env):
    app, *_ = env
    r = TestClient(app, base_url=BASE_URL).get("/auth/google/callback", follow_redirects=False)
    # No such provider configured here: a 404 JSON; the sign-in redirects
    # (lore.auth.signin._redirect) set no-referrer themselves.
    from lore.auth.signin import _redirect
    assert _redirect("/app").headers["referrer-policy"] == "no-referrer"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_hsts_only_behind_https(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions,
                   auth=make_settings(base_url="https://lore.example", cookie_secure=True))
    r = TestClient(app, base_url="https://lore.example").get("/signin")
    assert_secured(r, hsts=True)
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")


def test_local_mode_is_unchanged(tmp_path):
    from lore.web.app import create_app
    r = TestClient(create_app(LoreConfig(data_dir=tmp_path)), base_url=BASE_URL).get("/api/books")
    assert "content-security-policy" not in r.headers


def test_the_page_has_no_inline_script_or_style():
    html = (WEB / "index.html").read_text()
    for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
        assert not tag.strip(), "inline script in web/index.html"
    assert "<style" not in html
    config = (WEB / "vite.config.ts").read_text()
    assert 'tag: "style"' not in config  # the themes ship as a stylesheet file
    assert (WEB / "public" / "theme-boot.js").exists()


# --- sessions --------------------------------------------------------------------

def rows(sessions):
    with sessions() as db:
        return db.scalars(sa.select(AuthSession)).all()


def test_the_idle_timeout(env):
    app, sessions, clock = env
    client = sign_in(app, sessions, "alice")
    clock.now = NOW + auth_sessions.IDLE - timedelta(minutes=1)
    assert client.get("/api/me").status_code == 200   # used: slides 7 days on
    clock.now += auth_sessions.IDLE - timedelta(minutes=1)
    assert client.get("/api/me").status_code == 200
    clock.now += auth_sessions.IDLE + timedelta(minutes=1)  # 7 days unused
    assert client.get("/api/me").status_code == 401
    assert rows(sessions) == []  # and the row is gone


def test_the_absolute_cap_whatever_the_sliding_expiry_says(env):
    app, sessions, clock = env
    client = sign_in(app, sessions, "alice")
    for day in range(1, 30):  # used every day: never idle
        clock.now = NOW + timedelta(days=day)
        assert client.get("/api/me").status_code == 200, day
    # The sliding expiry never passes the 30-day cap...
    assert rows(sessions)[0].expires_at == NOW + auth_sessions.ABSOLUTE
    # ...and the cap holds even if a row says otherwise.
    with sessions() as db:
        db.scalars(sa.select(AuthSession)).one().expires_at = NOW + timedelta(days=90)
        db.commit()
    clock.now = NOW + auth_sessions.ABSOLUTE
    assert client.get("/api/me").status_code == 401
    assert rows(sessions) == []


def test_the_refreshed_cookie_lives_no_longer_than_the_session(env):
    app, sessions, clock = env
    client = sign_in(app, sessions, "alice")
    for day in range(1, 28):  # used daily, so never idle
        clock.now = NOW + timedelta(days=day)
        client.get("/api/me")
    clock.now = NOW + timedelta(days=28)
    r = client.get("/api/me")
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("lore_session="))
    assert "Max-Age=172800" in cookie  # two days left of the thirty


def test_sign_in_rotates_the_session_id(tmp_path):
    from tests.auth_fixtures import FakeMailer
    sessions, _ = central_sessions(tmp_path, users=())
    from lore.central.models import User
    with sessions() as db:
        db.add(User(id="alice", email="alice@example.org"))
        db.commit()
    mailer = FakeMailer()
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions, mailer=mailer)
    first = sign_in(app, sessions, "alice")
    old = first.session_token
    assert first.post("/auth/email", json={"email": "alice@example.org"}).status_code == 202
    token = mailer.links()[-1].split("token=")[1]
    r = first.post("/auth/email/verify", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303
    set_cookie = next(c for c in r.headers.get_list("set-cookie")
                      if c.startswith("lore_session="))
    new = set_cookie.split(";")[0].split("=", 1)[1]
    assert new and new != old
    from lore.auth.tokens import hash_token
    assert hash_token(old) not in {row.token_hash for row in rows(sessions)}
