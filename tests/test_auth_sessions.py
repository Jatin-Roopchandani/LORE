"""Sessions, the user hook, the cookie flags, logout and CSRF (all offline)."""
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth import sessions as auth_sessions
from lore.auth.tokens import hash_token
from lore.central.models import AuthSession, User
from lore.config import LoreConfig
from tests.auth_fixtures import BASE_URL, NOW, Clock, make_app, make_settings, sign_in
from tests.central_fixtures import central_sessions


@pytest.fixture
def env(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    clock = Clock()
    app = make_app(root, sessions, auth_clock=clock)
    return app, sessions, clock, root


def rows(sessions):
    with sessions() as db:
        return db.scalars(sa.select(AuthSession)).all()


def test_a_session_stores_only_the_hash_of_its_token(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    (row,) = rows(sessions)
    assert row.token_hash == hash_token(client.session_token)
    assert client.session_token not in (row.token_hash, row.user_id)
    assert len(client.session_token) >= 43  # 256 bits, base64url


def test_a_session_resolves_to_its_user(env):
    app, sessions, *_ = env
    r = sign_in(app, sessions, "alice").get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["id"] == "alice"


@pytest.mark.parametrize("cookie", [None, "", "nonsense", "x" * 500])
def test_a_missing_or_bad_cookie_is_401(env, cookie):
    app, *_ = env
    client = TestClient(app, base_url=BASE_URL)
    if cookie is not None:
        client.cookies.set("lore_session", cookie)
    assert client.get("/api/books").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_an_expired_session_is_401(env):
    app, sessions, clock, _ = env
    client = sign_in(app, sessions, "alice")
    assert client.get("/api/books").status_code == 200
    clock.now = NOW + auth_sessions.LIFETIME + timedelta(seconds=1)
    assert client.get("/api/books").status_code == 401


def test_the_expiry_slides_on_use_but_is_written_at_most_daily(env):
    app, sessions, clock, _ = env
    client = sign_in(app, sessions, "alice")
    first = rows(sessions)[0].expires_at
    clock.now = NOW + timedelta(hours=3)  # within a day: no write
    client.get("/api/books")
    assert rows(sessions)[0].expires_at == first
    clock.now = NOW + timedelta(days=2)   # past a day: rewritten
    r = client.get("/api/books")
    assert rows(sessions)[0].expires_at == clock.now + auth_sessions.LIFETIME
    # ...and the cookies are re-sent with the new lifetime.
    assert any("lore_session=" in c for c in r.headers.get_list("set-cookie"))
    clock.now = NOW + timedelta(days=2, hours=1)
    r = client.get("/api/books")
    assert "set-cookie" not in r.headers


def test_a_disabled_user_cannot_use_a_live_session(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    with sessions() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    assert client.get("/api/books").status_code == 401


def test_a_session_for_a_deleted_user_is_gone(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    with sessions() as db:
        db.execute(sa.text("PRAGMA foreign_keys=ON"))
        db.delete(db.get(User, "alice"))
        db.commit()
    assert client.get("/api/books").status_code == 401


def test_logout_deletes_the_row_and_clears_the_cookies(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert rows(sessions) == []
    cleared = " ".join(r.headers.get_list("set-cookie"))
    assert "lore_session=;" in cleared and "Max-Age=0" in cleared
    assert "lore_csrf=;" in cleared
    # The old cookie is now worthless, even if it is replayed.
    client.cookies.set("lore_session", client.session_token)
    assert client.get("/api/books").status_code == 401


def test_logout_needs_the_csrf_token(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    del client.headers["X-CSRF-Token"]
    assert client.post("/auth/logout").status_code == 403
    assert len(rows(sessions)) == 1


def test_one_users_session_does_not_sign_in_another(env):
    app, sessions, *_ = env
    alice = sign_in(app, sessions, "alice")
    bob = sign_in(app, sessions, "bob")
    assert alice.get("/api/me").json()["user"]["id"] == "alice"
    assert bob.get("/api/me").json()["user"]["id"] == "bob"


def test_cookie_flags(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    settings = make_settings(cookie_secure=True, base_url="https://lore.example.org")
    app = make_app(root, sessions, auth=settings)
    with sessions() as db:
        token = auth_sessions.create_session(db, "alice", NOW)
        db.commit()
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.set("lore_session", token)
    r = client.get("/api/me")  # no csrf cookie yet: it is issued here
    cookies = {c.split("=", 1)[0]: c for c in r.headers.get_list("set-cookie")}
    csrf = cookies["lore_csrf"]
    assert "Secure" in csrf and "SameSite=Lax" in csrf and "Path=/" in csrf
    assert "HttpOnly" not in csrf  # the page's script must read it

    # The session cookie itself is set by sign-in; check the shared builder.
    from lore.auth.cookies import session_headers
    text = session_headers(settings, "tok", "csrf", 2592000)[0][1].decode()
    assert text.startswith("lore_session=tok")
    for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=2592000"):
        assert flag in text


def test_secure_cookies_can_be_turned_off_for_http_dev():
    from lore.auth.cookies import session_headers
    text = session_headers(make_settings(cookie_secure=False), "t", "c", 5)[0][1].decode()
    assert "Secure" not in text and "HttpOnly" in text


def test_secure_cookies_require_an_https_base_url():
    from lore.auth.settings import AuthConfigError
    with pytest.raises(AuthConfigError):
        make_settings(cookie_secure=True, base_url="http://lore.example.org")


# --- CSRF ---

def test_a_mutation_without_the_token_is_403(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/worlds", json={"name": "Baker Street"}).status_code == 403


def test_a_mutation_with_a_wrong_token_is_403(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    real = client.headers["X-CSRF-Token"]
    bad = real[:-1] + ("1" if real[-1] == "0" else "0")   # always differs from the real token
    for token in (bad, "x", "0" * 64):
        r = client.post("/api/worlds", json={"name": "w"},
                        headers={"X-CSRF-Token": token})
        assert r.status_code == 403


def test_another_sessions_token_is_refused(env):
    app, sessions, *_ = env
    alice = sign_in(app, sessions, "alice")
    bob = sign_in(app, sessions, "bob")
    r = alice.post("/api/worlds", json={"name": "w"},
                   headers={"X-CSRF-Token": bob.headers["X-CSRF-Token"]})
    assert r.status_code == 403


def test_the_cookie_alone_is_not_enough(env):
    """Double submit means the HEADER must match: the cookie travels on a
    forged cross-site request, the header cannot."""
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    del client.headers["X-CSRF-Token"]
    assert client.cookies["lore_csrf"]
    assert client.post("/api/worlds", json={"name": "w"}).status_code == 403


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/worlds", {"name": "w"}),
    ("PATCH", "/api/worlds/1", {"name": "w"}),
    ("DELETE", "/api/worlds/1", None),
    ("POST", "/api/books/b1/continue", None),
    ("POST", "/api/books/b1/move", {"world": "x"}),
    ("POST", "/api/ask", {"question": "who?"}),
])
def test_every_mutating_api_route_enforces_csrf(env, method, path, body):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    del client.headers["X-CSRF-Token"]
    r = client.request(method, path, json=body)
    assert r.status_code == 403, (method, path, r.status_code)


def test_the_token_lets_the_mutation_through(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    r = client.post("/api/worlds", json={"name": "Baker Street"})
    assert r.status_code == 201


def test_reads_do_not_need_the_token(env):
    app, sessions, *_ = env
    client = sign_in(app, sessions, "alice")
    del client.headers["X-CSRF-Token"]
    assert client.get("/api/worlds").status_code == 200
    assert client.get("/api/library").status_code == 200


def test_every_mutating_route_of_the_app_is_covered_by_the_guard(env):
    """The guard is app-wide, so a route added later is covered too: assert
    it is installed as a dependency of the application itself."""
    app, *_ = env
    api = app.app.app  # OriginGuard -> SessionRefresh -> FastAPI
    from lore.auth.csrf import csrf_guard
    assert any(d.dependency is csrf_guard for d in api.router.dependencies)


def test_an_unsigned_mutation_is_401_not_a_csrf_error(env):
    app, *_ = env
    r = TestClient(app, base_url=BASE_URL).post("/api/worlds", json={"name": "w"})
    assert r.status_code == 401


def test_local_mode_needs_no_token_and_no_session(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    from lore.web.app import create_app
    client = TestClient(create_app(LoreConfig(data_dir=tmp_path)),
                        base_url=BASE_URL)
    assert client.post("/api/worlds", json={"name": "w"}).status_code == 201
    assert client.get("/api/me").status_code == 404  # no auth routes exist


def test_multi_mode_refuses_to_start_without_its_secrets(tmp_path, monkeypatch):
    from lore.auth.settings import AuthConfigError
    from lore.web.app import create_app
    for name in ("LORE_SESSION_SECRET", "LORE_CSRF_SECRET", "LORE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthConfigError):
        create_app(LoreConfig(data_dir=tmp_path), multi_user=True)


def test_settings_reject_short_or_equal_secrets():
    from lore.auth.settings import AuthConfigError, AuthSettings
    with pytest.raises(AuthConfigError):
        AuthSettings(session_secret="short", csrf_secret="c" * 40, cookie_secure=False)
    with pytest.raises(AuthConfigError):
        AuthSettings(session_secret="a" * 40, csrf_secret="a" * 40, cookie_secure=False)


def test_the_settings_repr_hides_secrets():
    text = repr(make_settings(resend_api_key="re_supersecret"))
    assert "re_supersecret" not in text and "s" * 40 not in text and "c" * 40 not in text


def test_the_public_address_is_answered_and_other_hosts_and_origins_are_not(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    settings = make_settings(cookie_secure=True, base_url="https://lore.example.org")
    app = make_app(root, sessions, auth=settings)
    public = TestClient(app, base_url="https://lore.example.org")
    assert public.get("/auth/providers").status_code == 200
    assert public.get("/auth/providers", headers={"Origin": "https://lore.example.org"}
                      ).status_code == 200
    assert public.get("/auth/providers", headers={"Origin": "https://evil.example"}
                      ).status_code == 403
    assert TestClient(app, base_url="https://evil.example").get(
        "/auth/providers").status_code == 403
