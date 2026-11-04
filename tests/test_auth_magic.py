"""Magic links: single use, expiry, no account oracle, rate limits, invites."""
import re
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from lore.auth import accounts
from lore.auth.tokens import hash_token
from lore.central.models import AuthSession, Identity, Invite, LoginToken, RateHit, User
from lore.config import LoreConfig
from tests.auth_fixtures import (
    BASE_URL, NOW, Clock, FakeMailer, FakeProvider, make_app, make_settings,
)
from tests.central_fixtures import central_sessions


@pytest.fixture
def env(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    mailer, clock = FakeMailer(), Clock()
    app = make_app(root, sessions, mailer=mailer, auth_clock=clock,
                   providers={"google": FakeProvider()})
    client = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    return client, sessions, mailer, clock, app


def ask_link(client, email, **extra):
    return client.post("/auth/email", json={"email": email, **extra})


def link_path(mailer) -> str:
    (link,) = mailer.links()[-1:]
    assert link.startswith(f"{BASE_URL}/auth/email/verify?token=")
    return link[len(BASE_URL):]


def make_invite(sessions):
    with sessions() as db:
        code = accounts.create_invite(db, "alice", NOW)
        db.commit()
    return code


def count(sessions, model):
    with sessions() as db:
        return db.scalar(sa.select(sa.func.count()).select_from(model))


def test_a_link_signs_an_existing_user_in(env):
    client, sessions, mailer, *_ = env
    r = ask_link(client, "Alice@Example.org ")
    assert r.status_code == 202 and r.json() == {"sent": True}
    (mail,) = mailer.sent
    assert mail["to"] == "alice@example.org" and "15 minutes" in mail["text"]
    r = client.get(link_path(mailer))
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/me").json()["user"]["id"] == "alice"
    with sessions() as db:
        (ident,) = db.scalars(sa.select(Identity)).all()
        assert (ident.provider, ident.provider_user_id) == ("email", "alice@example.org")


def test_a_link_works_once(env):
    client, sessions, mailer, *_ = env
    ask_link(client, "alice@example.org")
    path = link_path(mailer)
    assert client.get(path).headers["location"] == "/app"
    other = TestClient(client.app, base_url=BASE_URL, follow_redirects=False)
    assert other.get(path).headers["location"] == "/signin?error=link_invalid"
    assert other.get("/api/me").status_code == 401
    assert count(sessions, AuthSession) == 1


def test_a_link_expires_after_fifteen_minutes(env):
    client, _, mailer, clock, _ = env
    ask_link(client, "alice@example.org")
    clock.now = NOW + timedelta(minutes=15, seconds=1)
    r = client.get(link_path(mailer))
    assert r.headers["location"] == "/signin?error=link_invalid"
    assert client.get("/api/me").status_code == 401


def test_the_signature_also_carries_the_age(env, monkeypatch):
    """Even with the database row still valid, an old signed token is refused."""
    client, _, mailer, _, _ = env
    ask_link(client, "alice@example.org")
    path = link_path(mailer)
    real = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, "get_timestamp",
                        lambda self: real(self) + 16 * 60)
    assert client.get(path).headers["location"] == "/signin?error=link_invalid"


def test_a_just_valid_link_still_works(env):
    client, _, mailer, clock, _ = env
    ask_link(client, "alice@example.org")
    clock.now = NOW + timedelta(minutes=14, seconds=59)
    assert client.get(link_path(mailer)).headers["location"] == "/app"


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-3] + "abc", lambda t: "x" + t, lambda t: "", lambda t: "a.b.c",
    lambda t: t + "." + t])
def test_a_tampered_or_garbage_token_is_refused(env, mangle):
    client, sessions, mailer, *_ = env
    ask_link(client, "alice@example.org")
    token = parse_qs(urlparse(link_path(mailer)).query)["token"][0]
    r = client.get("/auth/email/verify", params={"token": mangle(token)})
    assert r.headers["location"] == "/signin?error=link_invalid"
    assert count(sessions, AuthSession) == 0


def test_the_missing_token_and_a_huge_one_are_refused(env):
    client, *_ = env
    assert client.get("/auth/email/verify").status_code in (303, 422)
    assert client.get("/auth/email/verify", params={"token": "a" * 5000}).status_code in (303, 422)


def test_a_token_signed_with_another_secret_is_refused(env):
    from itsdangerous import URLSafeTimedSerializer
    client, sessions, mailer, *_ = env
    forged = URLSafeTimedSerializer("z" * 40, salt="lore.magic-link").dumps("whatever")
    r = client.get("/auth/email/verify", params={"token": forged})
    assert r.headers["location"] == "/signin?error=link_invalid"


def test_the_token_is_stored_hashed(env):
    from itsdangerous import URLSafeTimedSerializer
    client, sessions, mailer, *_ = env
    ask_link(client, "alice@example.org")
    token = parse_qs(urlparse(link_path(mailer)).query)["token"][0]
    raw = URLSafeTimedSerializer("s" * 40, salt="lore.magic-link").loads(token)
    with sessions() as db:
        (row,) = db.scalars(sa.select(LoginToken)).all()
    assert row.token_hash == hash_token(raw)
    assert raw not in (row.token_hash, row.email) and token != row.token_hash


def test_a_link_for_a_known_and_an_unknown_address_looks_the_same(env):
    client, sessions, mailer, *_ = env
    known = ask_link(client, "alice@example.org")
    unknown = ask_link(client, "nobody@example.org")
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert dict(known.headers).keys() == dict(unknown.headers).keys()
    # The known address got its mail; the unknown one (no invite) got none.
    assert [m["to"] for m in mailer.sent] == ["alice@example.org"]


def test_a_malformed_address_is_400(env):
    client, *_ = env
    for bad in ("", "nope", "a@b", "a b@c.org", "x" * 400 + "@c.org"):
        assert ask_link(client, bad).status_code == 400


def test_a_mail_failure_does_not_change_the_answer(env):
    client, _, mailer, *_ = env
    mailer.fail = True
    r = ask_link(client, "alice@example.org")
    assert r.status_code == 202 and r.json() == {"sent": True}


def test_a_disabled_user_gets_no_link(env):
    client, sessions, mailer, *_ = env
    with sessions() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    assert ask_link(client, "alice@example.org").status_code == 202
    assert mailer.sent == []


# --- invites ---

def test_a_new_address_with_an_invite_gets_a_link_and_an_account(env):
    client, sessions, mailer, *_ = env
    code = make_invite(sessions)
    assert ask_link(client, "new@example.org", invite=code).status_code == 202
    assert [m["to"] for m in mailer.sent] == ["new@example.org"]
    assert client.get(link_path(mailer)).headers["location"] == "/app"
    me = client.get("/api/me").json()["user"]
    assert me["email"] == "new@example.org" and me["role"] == "user"
    with sessions() as db:
        assert db.get(Invite, hash_token(code)).used_by == me["id"]


def test_the_login_token_row_holds_the_invite_only_as_a_hash(env):
    client, sessions, mailer, *_ = env
    code = make_invite(sessions)
    ask_link(client, "new@example.org", invite=code)
    with sessions() as db:
        (row,) = db.scalars(sa.select(LoginToken)).all()
    assert row.invite_hash == hash_token(code) and code not in row.invite_hash


def test_the_invite_can_be_spent_before_the_link_is_opened(env):
    client, sessions, mailer, *_ = env
    code = make_invite(sessions)
    ask_link(client, "late@example.org", invite=code)
    with sessions() as db:  # someone else used it in the meantime
        db.get(Invite, hash_token(code)).used_by = "alice"
        db.commit()
    r = client.get(link_path(mailer))
    assert r.headers["location"] == "/waitlist"
    assert count(sessions, User) == 1 and count(sessions, AuthSession) == 0
    # The link is spent either way.
    assert client.get(link_path(mailer)).headers["location"] == "/signin?error=link_invalid"


def test_a_bad_invite_gets_no_mail(env):
    client, _, mailer, *_ = env
    assert ask_link(client, "new@example.org", invite="A" * 32).status_code == 202
    assert ask_link(client, "new2@example.org", invite="not an invite!").status_code == 202
    assert mailer.sent == []


def test_when_the_beta_is_open_anyone_may_sign_up(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=())
    mailer = FakeMailer()
    app = make_app(root, sessions, mailer=mailer, auth=make_settings(invite_only=False))
    client = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    ask_link(client, "open@example.org")
    assert client.get(link_path(mailer)).headers["location"] == "/app"


def test_an_existing_user_needs_no_invite_and_wastes_none(env):
    client, sessions, mailer, *_ = env
    code = make_invite(sessions)
    ask_link(client, "alice@example.org", invite=code)
    client.get(link_path(mailer))
    with sessions() as db:
        assert db.get(Invite, hash_token(code)).used_by is None


def test_the_same_email_by_link_and_by_oauth_is_one_user(env):
    client, sessions, mailer, *_ = env
    from lore.auth.providers import Profile
    provider = client.app.app.app.state.auth.providers["google"]
    provider.profile = Profile("g-alice", "alice@example.org", True, "Alice")
    r = client.get("/auth/google/start")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get("/auth/google/callback", params={"code": "c", "state": state})
    other = TestClient(client.app, base_url=BASE_URL, follow_redirects=False)
    ask_link(other, "alice@example.org")
    other.get(link_path(mailer))
    assert other.get("/api/me").json()["user"]["id"] == "alice"
    assert count(sessions, User) == 1 and count(sessions, Identity) == 2


# --- rate limits ---

def test_five_requests_per_email_per_hour(env):
    client, _, mailer, clock, _ = env
    for _ in range(5):
        assert ask_link(client, "alice@example.org").status_code == 202
    r = ask_link(client, "alice@example.org")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert len(mailer.sent) == 5
    # Case and spacing do not dodge it; another address is unaffected.
    assert ask_link(client, " ALICE@example.org").status_code == 429
    assert ask_link(client, "bob@example.org").status_code == 202
    clock.now = NOW + timedelta(minutes=61)  # the window slides
    assert ask_link(client, "alice@example.org").status_code == 202


def test_twenty_requests_per_ip_per_hour(env):
    client, _, _, clock, _ = env
    for n in range(20):
        assert ask_link(client, f"user{n}@example.org").status_code == 202
    assert ask_link(client, "user99@example.org").status_code == 429
    clock.now = NOW + timedelta(minutes=61)
    assert ask_link(client, "user99@example.org").status_code == 202


def test_the_forwarded_for_header_is_ignored_unless_a_proxy_is_trusted(env):
    client, *_ = env
    for n in range(20):
        ask_link_h = client.post("/auth/email", json={"email": f"u{n}@example.org"},
                                 headers={"X-Forwarded-For": f"10.0.0.{n}"})
        assert ask_link_h.status_code == 202
    r = client.post("/auth/email", json={"email": "z@example.org"},
                    headers={"X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429  # spoofing a new address did not help


def test_behind_a_trusted_proxy_the_last_forwarded_address_counts(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(root, sessions, mailer=FakeMailer(),
                   auth=make_settings(trust_proxy=True))
    client = TestClient(app, base_url=BASE_URL)
    for n in range(20):  # the client invents the first entry; Caddy appends the real one
        r = client.post("/auth/email", json={"email": f"u{n}@example.org"},
                        headers={"X-Forwarded-For": f"6.6.6.{n}, 203.0.113.7"})
        assert r.status_code == 202
    r = client.post("/auth/email", json={"email": "z@example.org"},
                    headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
    assert r.status_code == 429
    r = client.post("/auth/email", json={"email": "z@example.org"},
                    headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.8"})
    assert r.status_code == 202


def test_rate_limit_rows_hold_no_addresses(env):
    client, sessions, *_ = env
    ask_link(client, "alice@example.org")
    with sessions() as db:
        keys = [r.key for r in db.scalars(sa.select(RateHit))]
    assert keys and all("alice" not in k and "@" not in k for k in keys)


def test_old_rate_rows_and_spent_tokens_are_purged_at_sign_in(env):
    client, sessions, mailer, clock, _ = env
    ask_link(client, "alice@example.org")
    clock.now = NOW + timedelta(days=2)
    ask_link(client, "alice@example.org")
    client.get(link_path(mailer))  # sign-in housekeeping runs
    with sessions() as db:
        hits = db.scalars(sa.select(RateHit)).all()
    assert hits and all(r.at > NOW + timedelta(days=1) for r in hits)
    assert count(sessions, LoginToken) == 0  # the expired one and the spent one are gone


def test_the_email_endpoint_is_public_but_still_needs_json(env):
    client, *_ = env
    r = client.post("/auth/email", content="email=alice@example.org",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 422
