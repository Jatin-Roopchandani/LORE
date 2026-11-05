"""The waitlist, the admin API and `lore admin grant`. Mail is a fake."""
import re
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lore.auth import accounts
from lore.auth.tokens import hash_token
from lore.central.models import Invite, RateHit, User, Waitlist
from lore.cli import app as cli
from lore.config import LoreConfig
from tests.auth_fixtures import (
    BASE_URL, NOW, Clock, FakeMailer, FakeProvider, make_app, sign_in,
)
from tests.central_fixtures import central_sessions


@pytest.fixture
def env(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, url = central_sessions(tmp_path, users=("root", "alice"))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.commit()
    mailer, clock = FakeMailer(), Clock()
    app = make_app(root, sessions, mailer=mailer, auth_clock=clock,
                   providers={"google": FakeProvider()})
    return app, sessions, mailer, clock


def anon(app):
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


def join(client, **body):
    body = {"email": "reader@example.org", "reading": "Dracula", "consent": True, **body}
    return client.post("/api/waitlist", json=body)


def entries(sessions):
    with sessions() as db:
        return db.scalars(sa.select(Waitlist).order_by(Waitlist.id)).all()


# --- the public waitlist ---

def test_joining_the_waitlist_is_202_and_stored(env):
    app, sessions, *_ = env
    r = join(anon(app))
    assert r.status_code == 202
    (row,) = entries(sessions)
    assert (row.email, row.reading, row.status) == ("reader@example.org", "Dracula", "pending")


def test_it_needs_no_session_and_no_csrf_token(env):
    app, *_ = env
    assert join(anon(app)).status_code == 202


def test_a_filled_honeypot_is_202_and_stores_nothing(env):
    app, sessions, *_ = env
    assert join(anon(app), website="http://spam.example").status_code == 202
    assert entries(sessions) == []


@pytest.mark.parametrize("body", [{"consent": False}, {"email": "nonsense"}, {"email": ""}])
def test_no_consent_or_a_bad_address_is_202_and_stores_nothing(env, body):
    app, sessions, *_ = env
    assert join(anon(app), **body).status_code == 202
    assert entries(sessions) == []


def test_a_duplicate_or_an_existing_user_is_202_and_adds_nothing(env):
    app, sessions, *_ = env
    c = anon(app)
    join(c)
    join(c, email="READER@example.org")
    join(c, email="alice@example.org")  # already a user
    assert [e.email for e in entries(sessions)] == ["reader@example.org"]


def test_the_waitlist_is_rate_limited_per_ip_and_still_says_202(env):
    app, sessions, _, clock = env
    c = anon(app)
    for n in range(10):
        assert join(c, email=f"r{n}@example.org").status_code == 202
    assert join(c, email="late@example.org").status_code == 202
    assert len(entries(sessions)) == 10
    clock.now = NOW + timedelta(minutes=61)
    join(c, email="later@example.org")
    assert len(entries(sessions)) == 11


def test_the_reading_is_trimmed_to_a_sane_length(env):
    app, sessions, *_ = env
    join(anon(app), reading="x" * 5000)
    assert len(entries(sessions)[0].reading) == 500


def test_a_waitlist_ip_hit_is_hashed(env):
    app, sessions, *_ = env
    join(anon(app))
    with sessions() as db:
        assert all("127.0.0.1" not in r.key for r in db.scalars(sa.select(RateHit)))


# --- admin: who may call it ---

ADMIN_CALLS = [
    ("GET", "/api/admin/waitlist"), ("GET", "/api/admin/users"),
    ("POST", "/api/admin/waitlist/1/approve"), ("POST", "/api/admin/waitlist/1/decline"),
    ("POST", "/api/admin/invites"),
]


@pytest.mark.parametrize("method,path", ADMIN_CALLS)
def test_a_non_admin_gets_403(env, method, path):
    app, sessions, *_ = env
    r = sign_in(app, sessions, "alice").request(method, path)
    assert r.status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_CALLS)
def test_a_signed_out_caller_gets_401(env, method, path):
    app, *_ = env
    assert anon(app).request(method, path).status_code == 401


@pytest.mark.parametrize("method,path", [c for c in ADMIN_CALLS if c[0] == "POST"])
def test_admin_mutations_need_the_csrf_token_too(env, method, path):
    app, sessions, *_ = env
    admin = sign_in(app, sessions, "root")
    del admin.headers["X-CSRF-Token"]
    assert admin.request(method, path).status_code == 403


def test_a_disabled_admin_is_locked_out(env):
    app, sessions, *_ = env
    admin = sign_in(app, sessions, "root")
    with sessions() as db:
        db.get(User, "root").status = "disabled"
        db.commit()
    assert admin.get("/api/admin/users").status_code == 401


def test_demoting_an_admin_takes_effect_at_once(env):
    app, sessions, *_ = env
    admin = sign_in(app, sessions, "root")
    assert admin.get("/api/admin/users").status_code == 200
    with sessions() as db:
        db.get(User, "root").role = "user"
        db.commit()
    assert admin.get("/api/admin/users").status_code == 403


# --- admin: the work ---

def test_the_admin_lists_the_waitlist_pending_first(env):
    app, sessions, *_ = env
    c = anon(app)
    join(c, email="a@example.org")
    join(c, email="b@example.org")
    admin = sign_in(app, sessions, "root")
    admin.post("/api/admin/waitlist/1/decline")
    listed = admin.get("/api/admin/waitlist").json()["entries"]
    assert [(e["email"], e["status"]) for e in listed] == [
        ("b@example.org", "pending"), ("a@example.org", "declined")]


def test_approving_creates_an_invite_and_mails_a_sign_in_link(env):
    app, sessions, mailer, _ = env
    join(anon(app))
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/waitlist/1/approve")
    assert r.status_code == 200 and r.json()["entry"]["status"] == "invited"
    (mail,) = mailer.sent
    assert mail["to"] == "reader@example.org"
    (link,) = mailer.links()
    match = re.fullmatch(rf"{re.escape(BASE_URL)}/signin\?invite=([\w-]+)", link)
    assert match
    code = match.group(1)
    with sessions() as db:
        invite = db.get(Invite, hash_token(code))
        assert invite is not None and invite.created_by == "root" and invite.used_by is None
        assert code not in [i.code_hash for i in db.scalars(sa.select(Invite))]
    assert entries(sessions)[0].status == "invited" and entries(sessions)[0].invited_at


def test_the_mailed_invite_signs_the_person_up(env):
    app, sessions, mailer, _ = env
    join(anon(app))
    sign_in(app, sessions, "root").post("/api/admin/waitlist/1/approve")
    code = mailer.links()[0].split("invite=")[1]
    person = anon(app)
    from urllib.parse import parse_qs, urlparse
    # The sign-in page forwards the invite to the provider start...
    r = person.get(f"/auth/google/start?invite={code}")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = person.get("/auth/google/callback", params={"code": "c", "state": state})
    assert r.headers["location"] == "/app"
    assert person.get("/api/me").json()["user"]["email"] == "reader@example.org"


def test_a_failed_send_changes_nothing(env):
    app, sessions, mailer, _ = env
    join(anon(app))
    mailer.fail = True
    r = sign_in(app, sessions, "root").post("/api/admin/waitlist/1/approve")
    assert r.status_code == 502 and r.json()["error"]["code"] == "mail_failed"
    assert entries(sessions)[0].status == "pending"
    with sessions() as db:
        assert db.scalars(sa.select(Invite)).all() == []


def test_approving_twice_sends_one_invite(env):
    app, sessions, mailer, _ = env
    join(anon(app))
    admin = sign_in(app, sessions, "root")
    assert admin.post("/api/admin/waitlist/1/approve").status_code == 200
    assert admin.post("/api/admin/waitlist/1/approve").status_code == 409
    assert len(mailer.sent) == 1


def test_a_declined_request_can_be_approved_later(env):
    app, sessions, mailer, _ = env
    join(anon(app))
    admin = sign_in(app, sessions, "root")
    admin.post("/api/admin/waitlist/1/decline")
    assert entries(sessions)[0].status == "declined"
    assert admin.post("/api/admin/waitlist/1/approve").status_code == 200
    assert admin.post("/api/admin/waitlist/1/decline").status_code == 409  # already invited


def test_unknown_waitlist_ids_are_404(env):
    app, sessions, *_ = env
    admin = sign_in(app, sessions, "root")
    assert admin.post("/api/admin/waitlist/99/approve").status_code == 404
    assert admin.post("/api/admin/waitlist/99/decline").status_code == 404


def test_an_admin_creates_an_invite_to_hand_out(env):
    app, sessions, mailer, _ = env
    admin = sign_in(app, sessions, "root")
    body = admin.post("/api/admin/invites", json={"days": 3}).json()
    code = body["code"]
    assert body["link"] == f"{BASE_URL}/signin?invite={code}" and len(code) >= 43
    with sessions() as db:
        row = db.get(Invite, hash_token(code))
        assert row.expires_at == NOW + timedelta(days=3)
        assert db.get(Invite, code) is None  # not stored plain
    assert mailer.sent == []


def test_an_invite_with_no_body_gets_the_default_lifetime(env):
    app, sessions, *_ = env
    r = sign_in(app, sessions, "root").post("/api/admin/invites")
    assert r.status_code == 201
    assert r.json()["expires_at"].startswith("2026-11-18")


def test_an_absurd_lifetime_is_refused(env):
    app, sessions, *_ = env
    admin = sign_in(app, sessions, "root")
    assert admin.post("/api/admin/invites", json={"days": 0}).status_code == 422
    assert admin.post("/api/admin/invites", json={"days": 9999}).status_code == 422


def test_the_admin_lists_users(env):
    app, sessions, *_ = env
    users = sign_in(app, sessions, "root").get("/api/admin/users").json()["users"]
    assert {(u["email"], u["role"], u["status"]) for u in users} == {
        ("root@example.org", "admin", "active"), ("alice@example.org", "user", "active")}
    assert set(users[0]) == {"id", "email", "name", "role", "status", "created_at",
                             "has_key", "requests_today"}


# --- lore admin grant ---

def test_lore_admin_grant_promotes_and_creates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("LORE_DATABASE_URL", raising=False)
    (tmp_path / "data").mkdir()
    runner = CliRunner()
    assert runner.invoke(cli, ["migrate"]).exit_code == 0
    r = runner.invoke(cli, ["admin", "grant", "Boss@Example.org"])
    assert r.exit_code == 0 and "boss@example.org is now an admin (account created)" in r.output
    r = runner.invoke(cli, ["admin", "grant", "boss@example.org"])
    assert r.exit_code == 0 and "account created" not in r.output
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'data' / 'central.db'}")
    with engine.connect() as conn:
        rows = conn.execute(sa.text("select email, role, status from users")).all()
    assert rows == [("boss@example.org", "admin", "active")]
    assert runner.invoke(cli, ["admin", "grant", "not-an-email"]).exit_code == 2


def test_an_admin_granted_before_first_sign_in_gets_the_admin_account(env):
    app, sessions, mailer, _ = env
    with sessions() as db:
        accounts.grant_admin(db, "boss@example.org")
        db.commit()
    c = anon(app)
    c.post("/auth/email", json={"email": "boss@example.org"})  # no invite needed
    from tests.auth_fixtures import open_magic_link
    open_magic_link(c, mailer.links()[-1])
    assert c.get("/api/me").json()["user"]["role"] == "admin"
    assert c.get("/api/admin/users").status_code == 200
