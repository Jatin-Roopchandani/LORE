"""Invites tied to the approved email (release step 6, task 5.2).

An invite made by approving a waitlist entry works only for that address,
by OAuth and by magic link. A hand-out invite an admin creates stays unbound
unless an email is given."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth.providers import Profile
from lore.auth.tokens import hash_token
from lore.central.models import Invite, User
from lore.config import LoreConfig
from tests.auth_fixtures import (
    BASE_URL, Clock, FakeMailer, FakeProvider, make_app, open_magic_link, sign_in,
)
from tests.central_fixtures import central_sessions


@pytest.fixture
def env(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("root",))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.commit()
    mailer, provider = FakeMailer(), FakeProvider()
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions, mailer=mailer,
                   auth_clock=Clock(), providers={"google": provider})
    return app, sessions, mailer, provider


def anon(app):
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


def approved_code(app, sessions, mailer, email="reader@example.org"):
    anon(app).post("/api/waitlist", json={"email": email, "consent": True})
    assert sign_in(app, sessions, "root").post("/api/admin/waitlist/1/approve").status_code == 200
    return mailer.links()[-1].split("invite=")[1]


def oauth(app, provider, code, email):
    provider.profile = Profile(f"sub-{email}", email, True, "X")
    person = anon(app)
    r = person.get(f"/auth/google/start?invite={code}")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return person.get("/auth/google/callback", params={"code": "c", "state": state})


def test_an_approved_invite_is_bound_to_that_email(env):
    app, sessions, mailer, provider = env
    code = approved_code(app, sessions, mailer)
    with sessions() as db:
        assert db.get(Invite, hash_token(code)).email == "reader@example.org"


def test_someone_else_cannot_spend_an_approved_invite(env):
    app, sessions, mailer, provider = env
    code = approved_code(app, sessions, mailer)
    r = oauth(app, provider, code, "forwarded-to@example.org")
    assert r.headers["location"] == "/waitlist"
    with sessions() as db:
        assert db.get(Invite, hash_token(code)).used_by is None
        assert db.scalars(sa.select(User).where(User.email == "forwarded-to@example.org")).first() is None
    # ...and the person it was for still can.
    assert oauth(app, provider, code, "reader@example.org").headers["location"] == "/app"


def test_a_bound_invite_mails_no_link_to_another_address(env):
    app, sessions, mailer, provider = env
    code = approved_code(app, sessions, mailer)
    sent = len(mailer.sent)
    anon(app).post("/auth/email", json={"email": "other@example.org", "invite": code})
    assert len(mailer.sent) == sent  # no mail: the invite is not theirs
    client = anon(app)
    client.post("/auth/email", json={"email": "reader@example.org", "invite": code})
    assert open_magic_link(client, mailer.links()[-1]).headers["location"] == "/app"


def test_hand_out_invites_stay_unbound_unless_an_email_is_given(env):
    app, sessions, mailer, provider = env
    admin = sign_in(app, sessions, "root")
    free = admin.post("/api/admin/invites", json={}).json()["code"]
    bound = admin.post("/api/admin/invites", json={"email": "Pal@Example.org"}).json()["code"]
    assert admin.post("/api/admin/invites", json={"email": "nope"}).status_code == 400
    with sessions() as db:
        assert db.get(Invite, hash_token(free)).email is None
        assert db.get(Invite, hash_token(bound)).email == "pal@example.org"
    assert oauth(app, provider, free, "anyone@example.org").headers["location"] == "/app"
    assert oauth(app, provider, bound, "stranger@example.org").headers["location"] == "/waitlist"
    assert oauth(app, provider, bound, "pal@example.org").headers["location"] == "/app"
