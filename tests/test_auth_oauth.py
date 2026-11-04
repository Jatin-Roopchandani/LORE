"""OAuth sign-in with a fake provider, and the real providers on a mock
transport. No network."""
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth import accounts
from lore.auth.providers import _httpx as httpx
from lore.auth.providers import GitHubProvider, GoogleProvider, Profile, ProviderError
from lore.auth.settings import OAuthClient
from lore.central.models import AuthSession, Identity, Invite, User
from lore.config import LoreConfig
from tests.auth_fixtures import (
    BASE_URL, NOW, FakeProvider, make_app, make_settings,
)
from tests.central_fixtures import central_sessions


@pytest.fixture
def env(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    google, github = FakeProvider("google"), FakeProvider("github")
    app = make_app(root, sessions, providers={"google": google, "github": github})
    client = TestClient(app, base_url=BASE_URL, follow_redirects=False)
    return client, sessions, google, github


def make_invite(sessions, created_by="alice") -> str:
    with sessions() as db:
        code = accounts.create_invite(db, created_by, NOW)
        db.commit()
    return code


def begin(client, provider="google", invite=None):
    """Start a flow; returns the state the provider will echo back."""
    path = f"/auth/{provider}/start" + (f"?invite={invite}" if invite else "")
    r = client.get(path)
    assert r.status_code == 303
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def finish(client, provider="google", state=None, code="the-code"):
    params = {"code": code}
    if state is not None:
        params["state"] = state
    return client.get(f"/auth/{provider}/callback", params=params)


def users(sessions):
    with sessions() as db:
        return {u.email: u for u in db.scalars(sa.select(User))}


def count(sessions, model):
    with sessions() as db:
        return db.scalar(sa.select(sa.func.count()).select_from(model))


# --- the start of a flow ---

def test_start_redirects_with_state_and_a_pkce_verifier(env):
    client, _, google, _ = env
    r = client.get("/auth/google/start")
    assert r.status_code == 303 and r.headers["location"].startswith("https://provider.example/")
    (call,) = google.authorized
    assert call["redirect_uri"] == f"{BASE_URL}/auth/google/callback"
    assert len(call["state"]) >= 32 and len(call["code_verifier"]) >= 43
    flow = r.headers["set-cookie"]
    assert flow.startswith("lore_oauth=") and "HttpOnly" in flow
    assert "SameSite=Lax" in flow and "Path=/auth" in flow and "Max-Age=600" in flow


def test_every_start_has_its_own_state_and_verifier(env):
    client, _, google, _ = env
    client.get("/auth/google/start")
    client.get("/auth/google/start")
    a, b = google.authorized
    assert a["state"] != b["state"] and a["code_verifier"] != b["code_verifier"]


def test_an_unknown_provider_is_404(env):
    client, *_ = env
    assert client.get("/auth/myspace/start").status_code == 404
    assert client.get("/auth/myspace/callback?code=x&state=y").status_code == 404


def test_the_flow_cookie_is_secure_when_configured(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(root, sessions, providers={"google": FakeProvider()},
                   auth=make_settings(cookie_secure=True, base_url="https://lore.example.org"))
    r = TestClient(app, base_url=BASE_URL, follow_redirects=False).get("/auth/google/start")
    assert "Secure" in r.headers["set-cookie"]


def test_the_sign_in_page_lists_only_configured_providers(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _ = central_sessions(tmp_path, users=())
    app = make_app(root, sessions, providers={"github": FakeProvider("github")})
    body = TestClient(app, base_url=BASE_URL).get("/auth/providers").json()
    assert body == {"providers": ["github"], "email": True, "invite_only": True}


# --- state and PKCE are mandatory ---

def test_a_callback_without_state_is_refused_and_asks_nothing(env):
    client, sessions, google, _ = env
    begin(client)
    r = finish(client, state=None)
    assert r.headers["location"] == "/signin?error=oauth_failed"
    assert google.exchanged == [] and count(sessions, AuthSession) == 0


def test_a_callback_with_the_wrong_state_is_refused(env):
    client, _, google, _ = env
    begin(client)
    assert finish(client, state="forged").headers["location"] == "/signin?error=oauth_failed"
    assert google.exchanged == []


def test_a_callback_without_the_flow_cookie_is_refused(env):
    client, _, google, _ = env
    state = begin(client)
    client.cookies.clear()
    assert finish(client, state=state).headers["location"] == "/signin?error=oauth_failed"
    assert google.exchanged == []


def test_a_tampered_flow_cookie_is_refused(env):
    client, _, google, _ = env
    state = begin(client)
    client.cookies.set("lore_oauth", client.cookies["lore_oauth"][:-2] + "xx", path="/auth")
    assert finish(client, state=state).headers["location"] == "/signin?error=oauth_failed"
    assert google.exchanged == []


def test_a_flow_for_one_provider_cannot_finish_at_another(env):
    client, _, google, github = env
    state = begin(client, "google")
    assert finish(client, "github", state=state).headers["location"] == "/signin?error=oauth_failed"
    assert github.exchanged == []


def test_the_flow_cookie_is_single_use_in_effect(env):
    client, sessions, google, _ = env
    google.profile = Profile("sub-a", "alice@example.org", True, "Alice")
    state = begin(client)
    assert finish(client, state=state).headers["location"] == "/app"
    client.cookies.delete("lore_session")
    # The browser dropped the cleared cookie, so a replay has no flow.
    assert finish(client, state=state).headers["location"] == "/signin?error=oauth_failed"


def test_the_verifier_is_sent_to_the_token_exchange(env):
    client, _, google, _ = env
    google.profile = Profile("sub-a", "alice@example.org", True, None)
    state = begin(client)
    verifier = google.authorized[0]["code_verifier"]
    finish(client, state=state, code="abc")
    assert google.exchanged == [{"code": "abc", "code_verifier": verifier,
                                 "redirect_uri": f"{BASE_URL}/auth/google/callback"}]


def test_the_provider_saying_no_is_a_clean_failure(env):
    client, _, google, _ = env
    state = begin(client)
    r = client.get("/auth/google/callback", params={"error": "access_denied", "state": state})
    assert r.headers["location"] == "/signin?error=oauth_denied"
    state = begin(client)
    google.error = True
    assert finish(client, state=state).headers["location"] == "/signin?error=oauth_failed"


# --- who may sign in ---

def test_an_existing_user_signs_in_without_an_invite(env):
    client, sessions, google, _ = env
    google.profile = Profile("sub-a", "Alice@Example.org", True, "Alice A")
    r = finish(client, state=begin(client))
    assert r.headers["location"] == "/app"
    assert "lore_session=" in " ".join(r.headers.get_list("set-cookie"))
    assert count(sessions, User) == 1  # linked to the row that was there
    with sessions() as db:
        (ident,) = db.scalars(sa.select(Identity)).all()
        assert (ident.provider, ident.provider_user_id, ident.user_id) == (
            "google", "sub-a", "alice")
    assert client.get("/api/me").json()["user"]["id"] == "alice"


def test_a_new_user_needs_an_invite_and_lands_on_the_waitlist(env):
    client, sessions, google, _ = env
    r = finish(client, state=begin(client))
    assert r.headers["location"] == "/waitlist"
    assert "lore_session=" not in " ".join(r.headers.get_list("set-cookie"))
    assert "reader@example.org" not in users(sessions)
    assert count(sessions, Identity) == 0
    assert client.get("/api/me").status_code == 401


def test_a_new_user_with_an_invite_is_created_and_the_invite_is_spent(env):
    client, sessions, google, _ = env
    code = make_invite(sessions)
    r = finish(client, state=begin(client, invite=code))
    assert r.headers["location"] == "/app"
    new = users(sessions)["reader@example.org"]
    assert new.role == "user" and new.status == "active" and new.name == "Reader"
    with sessions() as db:
        assert db.get(Invite, accounts.hash_token(code)).used_by == new.id
    me = client.get("/api/me").json()["user"]
    assert me["email"] == "reader@example.org"


def test_an_invite_works_once(env):
    client, sessions, google, _ = env
    code = make_invite(sessions)
    finish(client, state=begin(client, invite=code))
    google.profile = Profile("sub-2", "second@example.org", True, None)
    other = TestClient(client.app, base_url=BASE_URL, follow_redirects=False)
    r = finish(other, state=begin(other, invite=code))
    assert r.headers["location"] == "/waitlist"
    assert "second@example.org" not in users(sessions)


def test_an_expired_or_made_up_invite_does_not_work(env):
    client, sessions, *_ = env
    from datetime import timedelta
    with sessions() as db:
        code = accounts.create_invite(db, "alice", NOW - timedelta(days=30),
                                      lifetime=timedelta(days=1))
        db.commit()
    for bad in (code, "A" * 32, "short", "../x"):
        r = finish(client, state=begin(client, invite=bad))
        assert r.headers["location"] == "/waitlist", bad
    assert "reader@example.org" not in users(sessions)


def test_the_invite_is_stored_only_as_a_hash(env):
    _, sessions, *_ = env
    code = make_invite(sessions)
    with sessions() as db:
        (row,) = db.scalars(sa.select(Invite)).all()
    assert row.code_hash == accounts.hash_token(code) and code not in row.code_hash


def test_an_unverified_email_is_rejected(env):
    client, sessions, google, _ = env
    code = make_invite(sessions)
    google.profile = Profile("sub-x", "alice@example.org", False, "Mallory")
    r = finish(client, state=begin(client, invite=code))
    assert r.headers["location"] == "/signin?error=email_unverified"
    assert count(sessions, AuthSession) == 0 and count(sessions, Identity) == 0
    # ...and it did not burn the invite or touch alice's account.
    with sessions() as db:
        assert db.scalars(sa.select(Invite)).one().used_by is None


def test_no_email_at_all_is_rejected(env):
    client, _, google, _ = env
    google.profile = Profile("sub-x", None, False, None)
    assert finish(client, state=begin(client)).headers["location"] == (
        "/signin?error=email_unverified")


def test_the_same_verified_email_across_providers_is_one_user(env):
    client, sessions, google, github = env
    code = make_invite(sessions)
    google.profile = Profile("g-1", "both@example.org", True, "Both")
    finish(client, state=begin(client, invite=code))
    first = users(sessions)["both@example.org"].id
    github.profile = Profile("gh-9", "BOTH@example.org", True, "both-login")
    other = TestClient(client.app, base_url=BASE_URL, follow_redirects=False)
    r = finish(other, "github", state=begin(other, "github"))
    assert r.headers["location"] == "/app"  # no invite needed: linked, not new
    assert users(sessions)["both@example.org"].id == first
    assert count(sessions, User) == 2  # alice and this one
    with sessions() as db:
        assert sorted(i.provider for i in db.scalars(
            sa.select(Identity).where(Identity.user_id == first))) == ["github", "google"]
    assert other.get("/api/me").json()["user"]["id"] == first


def test_a_known_identity_signs_in_even_if_the_email_changed(env):
    client, sessions, google, _ = env
    code = make_invite(sessions)
    finish(client, state=begin(client, invite=code))
    uid = users(sessions)["reader@example.org"].id
    google.profile = Profile("sub-1", "renamed@example.org", True, None)
    other = TestClient(client.app, base_url=BASE_URL, follow_redirects=False)
    assert finish(other, state=begin(other)).headers["location"] == "/app"
    assert other.get("/api/me").json()["user"]["id"] == uid


def test_a_disabled_user_cannot_sign_in(env):
    client, sessions, google, _ = env
    with sessions() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    google.profile = Profile("sub-a", "alice@example.org", True, None)
    r = finish(client, state=begin(client))
    assert r.headers["location"] == "/signin?error=account_disabled"
    assert count(sessions, AuthSession) == 0


def test_sign_in_replaces_a_planted_session(env):
    """A session id the browser held before sign-in is deleted, never promoted."""
    client, sessions, google, _ = env
    from tests.auth_fixtures import sign_in
    old = sign_in(client.app, sessions, "alice")
    client.cookies.set("lore_session", old.session_token)
    google.profile = Profile("sub-a", "alice@example.org", True, None)
    finish(client, state=begin(client))
    with sessions() as db:
        hashes = {s.token_hash for s in db.scalars(sa.select(AuthSession))}
    assert accounts.hash_token(old.session_token) not in hashes and len(hashes) == 1


def test_the_session_cookies_set_at_sign_in(env):
    client, _, google, _ = env
    google.profile = Profile("sub-a", "alice@example.org", True, None)
    r = finish(client, state=begin(client))
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("lore_session="))
    csrf = next(c for c in cookies if c.startswith("lore_csrf="))
    assert "HttpOnly" in session and "SameSite=Lax" in session and "Max-Age=2592000" in session
    assert "HttpOnly" not in csrf
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    # The CSRF cookie the browser now holds works for a mutation.
    token = client.cookies["lore_csrf"]
    assert client.post("/api/worlds", json={"name": "w"},
                       headers={"X-CSRF-Token": token}).status_code == 201


# --- the real providers, on a mock transport ---

def transport(routes, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for (method, url), answer in routes.items():
            if request.method == method and str(request.url).startswith(url):
                return httpx.Response(answer[0], json=answer[1])
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


CLIENT = OAuthClient("cid", "csecret")


def test_google_authorize_url_carries_state_and_a_pkce_challenge():
    url = GoogleProvider(CLIENT).authorize_url(
        redirect_uri="https://lore.example.org/auth/google/callback", state="st",
        code_verifier="v" * 64)
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["state"] == ["st"] and q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"][0] and q["code_challenge"][0] != "v" * 64
    assert q["scope"] == ["openid email profile"] and q["response_type"] == ["code"]
    assert q["client_id"] == ["cid"]
    assert "csecret" not in url


def test_github_authorize_url_asks_for_the_right_scopes():
    url = GitHubProvider(CLIENT).authorize_url(
        redirect_uri="https://lore.example.org/auth/github/callback", state="st",
        code_verifier="v" * 64)
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["read:user user:email"] and q["code_challenge_method"] == ["S256"]


def test_google_profile_and_the_code_verifier_reach_the_token_endpoint():
    seen = []
    provider = GoogleProvider(CLIENT, transport=transport({
        ("POST", "https://oauth2.googleapis.com/token"): (200, {
            "access_token": "at", "token_type": "Bearer"}),
        ("GET", "https://openidconnect.googleapis.com/v1/userinfo"): (200, {
            "sub": "123", "email": "g@example.org", "email_verified": True, "name": "G"}),
    }, seen))
    profile = provider.fetch_profile(code="the-code", code_verifier="v" * 64,
                                     redirect_uri="https://lore.example.org/cb")
    assert profile == Profile("123", "g@example.org", True, "G")
    form = parse_qs(seen[0].content.decode())
    assert form["code"] == ["the-code"] and form["code_verifier"] == ["v" * 64]
    assert form["grant_type"] == ["authorization_code"]
    assert seen[1].headers["authorization"] == "Bearer at"


@pytest.mark.parametrize("flag,expected", [(False, False), ("false", False),
                                           (None, False), ("true", True)])
def test_google_email_verified_must_be_true(flag, expected):
    info = {"sub": "1", "email": "g@example.org"}
    if flag is not None:
        info["email_verified"] = flag
    provider = GoogleProvider(CLIENT, transport=transport({
        ("POST", "https://oauth2.googleapis.com/token"): (200, {
            "access_token": "at", "token_type": "Bearer"}),
        ("GET", "https://openidconnect.googleapis.com/v1/userinfo"): (200, info),
    }, []))
    assert provider.fetch_profile(code="c", code_verifier="v" * 64,
                                  redirect_uri="https://x/cb").email_verified is expected


GITHUB_TOKEN = ("POST", "https://github.com/login/oauth/access_token")


def github(emails, user=None):
    return GitHubProvider(CLIENT, transport=transport({
        GITHUB_TOKEN: (200, {"access_token": "gat", "token_type": "bearer"}),
        ("GET", "https://api.github.com/user/emails"): (200, emails),
        ("GET", "https://api.github.com/user"): (200, user or {
            "id": 77, "login": "octo", "name": None, "email": "public@example.org"}),
    }, []))


def test_github_uses_the_primary_verified_email_only():
    profile = github([
        {"email": "other@example.org", "primary": False, "verified": True},
        {"email": "primary@example.org", "primary": True, "verified": True},
    ]).fetch_profile(code="c", code_verifier="v" * 64, redirect_uri="https://x/cb")
    assert profile == Profile("77", "primary@example.org", True, "octo")


def test_github_unverified_primary_is_not_trusted():
    profile = github([
        {"email": "primary@example.org", "primary": True, "verified": False},
        {"email": "other@example.org", "primary": False, "verified": True},
    ]).fetch_profile(code="c", code_verifier="v" * 64, redirect_uri="https://x/cb")
    assert profile.email is None and profile.email_verified is False
    # The public profile email, which GitHub does not verify, is never used.
    assert "public@example.org" not in repr(profile)


def test_a_token_endpoint_error_is_a_provider_error_without_the_response_text():
    provider = GoogleProvider(CLIENT, transport=transport({
        ("POST", "https://oauth2.googleapis.com/token"): (400, {
            "error": "invalid_grant", "error_description": "secret-ish detail"}),
    }, []))
    with pytest.raises(ProviderError) as exc:
        provider.fetch_profile(code="c", code_verifier="v" * 64, redirect_uri="https://x/cb")
    assert "secret-ish" not in str(exc.value)


def test_provider_reprs_do_not_show_the_client_secret():
    assert "csecret" not in repr(GoogleProvider(CLIENT)) + repr(CLIENT)
