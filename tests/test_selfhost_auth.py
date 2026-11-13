"""Self-host mode: password sign-in and its limits, people added and reset by
the admin, set-password links, changing my own password."""
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from lore.auth.passwords import hash_password, verify_password
from lore.central.models import AuthSession, RateHit, User
from lore.config import LoreConfig
from tests.auth_fixtures import BASE_URL, Clock, make_app, make_settings, sign_in
from tests.central_fixtures import central_sessions

PASSWORD = "correct horse battery"


@pytest.fixture
def env(tmp_path):
    """A set-up self-hosted server: admin `root` and user `alice`, both with
    passwords."""
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=("root", "alice"))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        for uid in ("root", "alice"):
            db.get(User, uid).password_hash = hash_password(PASSWORD)
        db.commit()
    clock = Clock()
    app = make_app(root, sessions, selfhost=True, auth=make_settings(selfhost=True),
                   providers={}, auth_clock=clock)
    return app, sessions, clock


def anon(app):
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


def password_in(client, email="alice@example.org", password=PASSWORD):
    r = client.post("/auth/password", json={"email": email, "password": password})
    if r.status_code == 200:
        client.headers["X-CSRF-Token"] = client.cookies.get(CSRF_COOKIE)
    return r


def user(sessions, email):
    with sessions() as db:
        return db.scalars(sa.select(User).where(User.email == email)).first()


# --- signing in ------------------------------------------------------------

def test_sign_in_with_a_password(env):
    app, sessions, _ = env
    client = anon(app)
    r = password_in(client)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "redirect": "/app"}
    assert SESSION_COOKIE in r.cookies
    assert client.get("/api/me").json()["user"]["id"] == "alice"


@pytest.mark.parametrize("email,password", [
    ("alice@example.org", "wrong password!"), ("nobody@example.org", PASSWORD),
    ("alice@example.org", ""), ("not an email", PASSWORD)])
def test_a_wrong_password_and_an_unknown_address_look_the_same(env, email, password):
    app, *_ = env
    r = anon(app).post("/auth/password", json={"email": email, "password": password})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    assert SESSION_COOKIE not in r.cookies


def test_a_disabled_account_cannot_sign_in(env):
    app, sessions, _ = env
    with sessions() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    assert password_in(anon(app)).status_code == 401


def test_sign_in_is_rate_limited_per_address(env):
    app, *_ = env
    client = anon(app)
    for _ in range(10):
        assert password_in(client, password="wrong password!").status_code == 401
    r = password_in(client)  # even the right password, once over the limit
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    # Another address from the same client is not locked out by alice's limit.
    assert password_in(client, email="root@example.org").status_code == 200


def test_the_limit_window_slides(env):
    from datetime import timedelta
    app, _sessions, clock = env
    client = anon(app)
    for _ in range(10):
        password_in(client, password="wrong password!")
    assert password_in(client).status_code == 429
    clock.now += timedelta(minutes=16)
    assert password_in(client).status_code == 200


def test_sign_in_is_rate_limited_per_client(env):
    app, sessions, _ = env
    client = anon(app)
    for i in range(60):
        client.post("/auth/password", json={"email": f"x{i}@example.org", "password": "nope nope"})
    assert password_in(client).status_code == 429
    with sessions() as db:  # counted under hashed keys, never the address
        keys = db.scalars(sa.select(RateHit.key)).all()
    assert keys and not any("example.org" in k for k in keys)


def test_signing_in_needs_no_csrf_token_but_changing_things_does(env):
    app, *_ = env
    client = anon(app)
    assert password_in(client).status_code == 200
    del client.headers["X-CSRF-Token"]
    r = client.post("/api/me/password", json={"current": PASSWORD, "password": "a new password"})
    assert r.status_code == 403


def test_another_site_cannot_post_a_sign_in(env):
    app, *_ = env
    r = anon(app).post("/auth/password", json={"email": "alice@example.org", "password": PASSWORD},
                       headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_sign_in_page_offers_passwords_and_no_mail(env):
    app, *_ = env
    client = anon(app)
    body = client.get("/auth/providers").json()
    assert body["password"] is True and body["email"] is False and body["providers"] == []
    assert client.get("/signin").status_code == 200


# --- my own password -------------------------------------------------------

def test_change_my_password(env):
    app, sessions, _ = env
    other_device = sign_in(app, sessions, "alice")
    client = anon(app)
    password_in(client)
    r = client.post("/api/me/password", json={"current": PASSWORD, "password": "a brand new one"})
    assert r.status_code == 200, r.text
    assert verify_password(user(sessions, "alice@example.org").password_hash, "a brand new one")
    assert client.get("/api/me").status_code == 200          # this device stays in
    assert other_device.get("/api/me").status_code == 401    # the others are out
    assert password_in(anon(app)).status_code == 401
    assert password_in(anon(app), password="a brand new one").status_code == 200


def test_changing_my_password_needs_the_current_one(env):
    app, sessions, _ = env
    client = anon(app)
    password_in(client)
    r = client.post("/api/me/password", json={"current": "not it at all", "password": "a brand new one"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "wrong_password"
    r = client.post("/api/me/password", json={"current": PASSWORD, "password": "short"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "weak_password"
    assert verify_password(user(sessions, "alice@example.org").password_hash, PASSWORD)


# --- the admin adds people -------------------------------------------------

def test_the_admin_adds_someone_with_a_password(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/users", json={"email": "Bob@Example.org", "name": "Bob",
                                             "password": "bobs password"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "bob@example.org" and body["user"]["role"] == "user"
    assert body["user"]["has_password"] is True and "link" not in body
    assert password_in(anon(app), "bob@example.org", "bobs password").status_code == 200
    listed = {u["email"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert listed["bob@example.org"]["has_password"] is True


def test_the_admin_adds_someone_with_a_set_password_link(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/users", json={"email": "carol@example.org"})
    assert r.status_code == 201
    link = r.json()["link"]
    assert r.json()["user"]["has_password"] is False
    code = parse_qs(urlparse(link["link"]).query)["code"][0]
    assert link["code"] == code and urlparse(link["link"]).path == "/set-password"
    visitor = anon(app)
    assert visitor.get("/set-password", params={"code": code}).status_code == 200
    assert visitor.get("/auth/password/link", params={"code": code}).json() == {
        "email": "carol@example.org"}
    r = visitor.post("/auth/password/link", json={"code": code, "password": "carols password"})
    assert r.status_code == 200 and r.json()["redirect"] == "/app"
    visitor.headers["X-CSRF-Token"] = visitor.cookies.get(CSRF_COOKIE)
    assert visitor.get("/api/me").json()["user"]["email"] == "carol@example.org"
    # Once only.
    again = anon(app).post("/auth/password/link", json={"code": code, "password": "someone else"})
    assert again.status_code == 400 and again.json()["error"]["code"] == "link_invalid"
    assert password_in(anon(app), "carol@example.org", "carols password").status_code == 200


def test_a_link_cannot_set_a_weak_password_or_be_guessed(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    code = admin.post("/api/admin/users", json={"email": "dan@example.org"}).json()["link"]["code"]
    r = anon(app).post("/auth/password/link", json={"code": code, "password": "short"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "weak_password"
    r = anon(app).post("/auth/password/link", json={"code": "x" * 43, "password": "long enough!"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "link_invalid"
    assert anon(app).get("/auth/password/link", params={"code": "x" * 43}).status_code == 404


def test_adding_an_existing_address_is_refused(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/users", json={"email": "alice@example.org", "password": "whatever123"})
    assert r.status_code == 409


def test_only_the_admin_adds_or_resets_people(env):
    app, sessions, _ = env
    alice = sign_in(app, sessions, "alice")
    assert alice.post("/api/admin/users", json={"email": "eve@example.org",
                                                "password": "eves password"}).status_code == 403
    assert alice.post("/api/admin/users/root/password",
                      json={"password": "taken over!!"}).status_code == 403
    assert anon(app).post("/api/admin/users", json={"email": "eve@example.org"}).status_code == 401
    assert user(sessions, "eve@example.org") is None


def test_the_admin_resets_a_password(env):
    app, sessions, _ = env
    alice_device = sign_in(app, sessions, "alice")
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/users/alice/password", json={"password": "reset by root"})
    assert r.status_code == 200, r.text
    assert alice_device.get("/api/me").status_code == 401  # signed out everywhere
    assert password_in(anon(app)).status_code == 401
    assert password_in(anon(app), password="reset by root").status_code == 200


def test_the_admin_resets_with_a_link(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/users/alice/password", json={})
    code = r.json()["link"]["code"]
    assert anon(app).post("/auth/password/link",
                          json={"code": code, "password": "alice chose this"}).status_code == 200
    assert password_in(anon(app), password="alice chose this").status_code == 200


def test_the_admin_resets_their_own_in_settings_not_here(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    assert admin.post("/api/admin/users/root/password",
                      json={"password": "something long"}).status_code == 409
    with sessions() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(AuthSession)
                         .where(AuthSession.user_id == "root")) == 1


def test_a_self_hosted_server_has_no_waitlist(env):
    app, *_ = env
    r = anon(app).post("/api/waitlist", json={"email": "x@example.org", "consent": True})
    assert r.status_code == 404
