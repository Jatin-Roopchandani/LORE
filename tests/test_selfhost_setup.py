"""Self-host mode: the first-run admin, created exactly once, and the gate
that holds every other route until it exists."""
import threading

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth.cookies import SESSION_COOKIE
from lore.auth.firstrun import SetupDone, create_first_admin, setup_needed
from lore.auth.passwords import hash_password
from lore.central.models import ServerSetting, User
from lore.config import LoreConfig
from tests.auth_fixtures import BASE_URL, NOW, make_app, make_settings
from tests.central_fixtures import central_sessions

PASSWORD = "correct horse battery"


def selfhost_app(tmp_path, users=()):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=users)
    app = make_app(root, sessions, selfhost=True, auth=make_settings(selfhost=True),
                   providers={})
    return app, sessions


def anon(app):
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


def admins(sessions):
    with sessions() as db:
        return db.scalars(sa.select(User).where(User.role == "admin")).all()


def setup(client, email="owner@example.org", password=PASSWORD, confirm=None):
    return client.post("/api/setup", json={"email": email, "password": password,
                                           "confirm": password if confirm is None else confirm})


# --- the gate --------------------------------------------------------------

def test_a_fresh_server_needs_setup(tmp_path):
    app, _ = selfhost_app(tmp_path)
    assert anon(app).get("/api/setup").json() == {"needed": True}


@pytest.mark.parametrize("path", ["/", "/app", "/admin", "/signin", "/set-password"])
def test_every_page_goes_to_setup_until_the_admin_exists(tmp_path, path):
    app, _ = selfhost_app(tmp_path)
    r = anon(app).get(path)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/books"), ("GET", "/api/me"), ("POST", "/auth/password"),
    ("GET", "/auth/providers"), ("GET", "/api/admin/users"), ("PUT", "/api/admin/provider"),
    ("POST", "/api/admin/users"), ("GET", "/api/library")])
def test_every_api_call_is_refused_until_the_admin_exists(tmp_path, method, path):
    app, _ = selfhost_app(tmp_path)
    r = anon(app).request(method, path, json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "setup_required"


def test_the_setup_screen_and_the_health_probe_stay_open(tmp_path):
    app, _ = selfhost_app(tmp_path)
    client = anon(app)
    assert client.get("/healthz").status_code == 200
    page = client.get("/setup")
    assert page.status_code == 200
    assert 'name="lore-selfhost"' in page.text or "not built" in page.text.lower()


# --- creating the admin ----------------------------------------------------

def test_setup_creates_the_admin_and_signs_them_in(tmp_path):
    app, sessions = selfhost_app(tmp_path)
    client = anon(app)
    r = setup(client)
    assert r.status_code == 201, r.text
    assert r.json()["redirect"] == "/app"
    assert SESSION_COOKIE in r.cookies
    (admin,) = admins(sessions)
    assert admin.email == "owner@example.org" and admin.status == "active"
    assert admin.password_hash and PASSWORD not in admin.password_hash
    client.headers["X-CSRF-Token"] = client.cookies.get("lore_csrf")
    me = client.get("/api/me")
    assert me.status_code == 200 and me.json()["user"]["role"] == "admin"
    assert client.get("/api/setup").json() == {"needed": False}


def test_after_setup_the_gate_opens_and_setup_is_closed(tmp_path):
    app, sessions = selfhost_app(tmp_path)
    assert setup(anon(app)).status_code == 201
    other = anon(app)
    again = setup(other, email="intruder@example.org")
    assert again.status_code == 409 and again.json()["error"]["code"] == "setup_done"
    assert [a.email for a in admins(sessions)] == ["owner@example.org"]
    # Signed out: pages go to sign-in now, not to setup.
    assert other.get("/").headers["location"] == "/signin"
    assert other.get("/setup").headers["location"] == "/signin"
    assert other.get("/api/books").status_code == 401


@pytest.mark.parametrize("body,code", [
    ({"email": "not-an-email", "password": PASSWORD, "confirm": PASSWORD}, "invalid_email"),
    ({"email": "o@example.org", "password": PASSWORD, "confirm": "different!!"},
     "password_mismatch"),
    ({"email": "o@example.org", "password": "short", "confirm": "short"}, "weak_password"),
])
def test_setup_refuses_a_bad_form_and_stays_open(tmp_path, body, code):
    app, sessions = selfhost_app(tmp_path)
    r = anon(app).post("/api/setup", json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert admins(sessions) == []
    assert anon(app).get("/api/setup").json() == {"needed": True}


def test_a_server_with_an_admin_already_never_offers_setup(tmp_path):
    app, sessions = selfhost_app(tmp_path, users=("root",))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.commit()
    assert anon(app).get("/api/setup").json() == {"needed": False}
    assert setup(anon(app)).status_code == 409


def test_concurrent_setups_make_exactly_one_admin(tmp_path):
    """The race: many first-run submissions at once, one admin."""
    _app, sessions = selfhost_app(tmp_path)
    hashed = hash_password(PASSWORD)
    barrier = threading.Barrier(8)
    won, lost = [], []

    def attempt(i):
        barrier.wait()
        try:
            won.append(create_first_admin(sessions, f"owner{i}@example.org", hashed, NOW))
        except SetupDone:
            lost.append(i)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and len(lost) == 7
    assert [a.id for a in admins(sessions)] == won
    with sessions() as db:
        assert not setup_needed(db)
        assert db.get(ServerSetting, "setup") is not None


def test_concurrent_setup_requests_make_exactly_one_admin(tmp_path):
    """The same race through HTTP: one 201, every other request 409."""
    app, sessions = selfhost_app(tmp_path)
    barrier = threading.Barrier(6)
    codes = []

    def attempt(i):
        client = anon(app)
        barrier.wait()
        codes.append(setup(client, email=f"owner{i}@example.org").status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [201] + [409] * 5
    assert len(admins(sessions)) == 1


def test_the_setup_marker_is_what_makes_it_once(tmp_path, monkeypatch):
    """Even when the check sees no admin yet (Postgres, read committed: the
    winner has not committed), the marker's primary key refuses the loser."""
    from lore.auth import firstrun
    _app, sessions = selfhost_app(tmp_path)
    with sessions() as db:
        db.add(ServerSetting(key="setup", value={}, updated_at=NOW))
        db.commit()
    monkeypatch.setattr(firstrun, "setup_needed", lambda db: True)
    with pytest.raises(SetupDone):
        create_first_admin(sessions, "late@example.org", hash_password(PASSWORD), NOW)
    assert admins(sessions) == []
