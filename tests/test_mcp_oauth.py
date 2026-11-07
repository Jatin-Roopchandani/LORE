"""OAuth 2.1 for MCP clients (claude.ai / Claude Desktop custom connectors):
the whole flow with a fake client, and every way it must refuse."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.auth.tokens import hash_token
from lore.central.models import OAuthClient, OAuthCode, OAuthGrant, OAuthToken
from lore.config import LoreConfig
from lore.mcp.oauth import redirect_uri_problem
from lore.tenancy import config_for
from tests.auth_fixtures import BASE_URL, FakeMailer, make_app, open_magic_link, sign_in
from tests.central_fixtures import central_sessions
from tests.mcp_fixtures import build_store_triple
from tests.test_mcp_hosted import call, code, rpc

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = f"{BASE_URL}/mcp"


class WallClock:
    """The OAuth server's clock: the real time, moved on by hand."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def root(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    build_store_triple(root.data_dir, books=[("ab", "A Study in Scarlet")],
                       entities=[("ae1", "Holmes", "Character", ["ab"], [])],
                       data_dir=config_for(root, "alice").data_dir)
    build_store_triple(root.data_dir, books=[("bb", "Dracula")],
                       entities=[("be1", "Count Dracula", "Character", ["bb"], [])],
                       data_dir=config_for(root, "bob").data_dir)
    return root


@pytest.fixture
def central(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return sessions


@pytest.fixture
def clock():
    return WallClock()


@pytest.fixture
def app(root, central, clock):
    return make_app(root, central, oauth_clock=clock)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def register(c, **over):
    body = {"redirect_uris": [REDIRECT], "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"]}
    body.update(over)
    return c.post("/register", json=body)


def authorize(c, client_id, challenge, *, state="st8", redirect=REDIRECT,
              resource=RESOURCE, method="S256", drop=()):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": method,
              "state": state, "resource": resource}
    for key in drop:
        params.pop(key)
    return c.get("/authorize", params={k: v for k, v in params.items() if v is not None},
                 follow_redirects=False)


def consent_req(response) -> str:
    assert response.status_code == 302, response.text
    url = urlparse(response.headers["location"])
    assert url.path == "/oauth/consent"
    return parse_qs(url.query)["req"][0]


def approve(user, req, yes=True) -> dict:
    r = user.post("/api/oauth/consent", json={"req": req, "approve": yes})
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["redirect"])
    return {"base": f"{url.scheme}://{url.netloc}{url.path}",
            **{k: v[0] for k, v in parse_qs(url.query).items()}}


def exchange(c, client_id, the_code, verifier, redirect=REDIRECT, **extra):
    return c.post("/token", data={"grant_type": "authorization_code", "code": the_code,
                                  "redirect_uri": redirect, "client_id": client_id,
                                  "code_verifier": verifier, **extra})


def refresh(c, client_id, token, **extra):
    return c.post("/token", data={"grant_type": "refresh_token", "refresh_token": token,
                                  "client_id": client_id, **extra})


def connect(c, app, central, user="alice", **reg) -> dict:
    """register -> authorize -> consent (as `user`) -> code -> tokens."""
    client_id = register(c, **reg).json()["client_id"]
    verifier, challenge = pkce()
    req = consent_req(authorize(c, client_id, challenge))
    got = approve(sign_in(app, central, user), req)
    tokens = exchange(c, client_id, got["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    return {"client_id": client_id, **tokens.json()}


# --- metadata -----------------------------------------------------------------------

def test_authorization_server_metadata(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == BASE_URL
    assert meta["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert meta["token_endpoint"] == f"{BASE_URL}/token"
    assert meta["registration_endpoint"] == f"{BASE_URL}/register"
    assert meta["revocation_endpoint"] == f"{BASE_URL}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"}


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource",
                                  "/.well-known/oauth-protected-resource/mcp"])
def test_protected_resource_metadata(client, path):
    meta = client.get(path).json()
    assert meta["resource"] == RESOURCE
    assert [s.rstrip("/") for s in meta["authorization_servers"]] == [BASE_URL]


# --- the whole flow -----------------------------------------------------------------

def test_register_authorize_consent_token_call_refresh_revoke(client, app, central):
    reg = register(client)
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge, state="abc"))

    alice = sign_in(app, central, "alice")
    shown = alice.get("/api/oauth/consent", params={"req": req}).json()
    assert shown == {"client_name": "Claude", "redirect_host": "claude.ai"}
    got = approve(alice, req)
    assert got["base"] == REDIRECT and got["state"] == "abc" and got["code"]

    tokens = exchange(client, client_id, got["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    assert body["access_token"].startswith("lore_at_") and body["refresh_token"]

    out = call(client, body["access_token"], "library")
    assert [b["title"] for b in out["books"]] == ["A Study in Scarlet"]

    again = refresh(client, client_id, body["refresh_token"])
    assert again.status_code == 200, again.text
    fresh = again.json()
    assert fresh["access_token"] != body["access_token"]
    assert fresh["refresh_token"] != body["refresh_token"]
    assert rpc(client, body["access_token"], "tools/list").status_code == 401  # rotated out
    assert call(client, fresh["access_token"], "library")["books"]

    listed = alice.get("/api/me/mcp-connections").json()["connections"]
    assert [c["client_name"] for c in listed] == ["Claude"]

    gone = client.post("/revoke", data={"token": fresh["refresh_token"],
                                        "client_id": client_id, "client_secret": ""})
    assert gone.status_code == 200
    assert rpc(client, fresh["access_token"], "tools/list").status_code == 401
    assert refresh(client, client_id, fresh["refresh_token"]).status_code == 400
    assert alice.get("/api/me/mcp-connections").json()["connections"] == []


def test_a_confidential_client_authenticates_and_its_secret_is_sealed(client, app, central):
    reg = register(client, token_endpoint_auth_method="client_secret_post").json()
    secret = reg["client_secret"]
    with central() as db:
        row = db.get(OAuthClient, reg["client_id"])
        assert row.secret_ciphertext and secret.encode() not in row.secret_ciphertext
        assert secret not in str(row.info)
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    got = approve(sign_in(app, central, "alice"), req)
    assert exchange(client, reg["client_id"], got["code"], verifier,
                    client_secret="wrong").status_code == 401
    ok = exchange(client, reg["client_id"], got["code"], verifier, client_secret=secret)
    assert ok.status_code == 200, ok.text


def test_tokens_codes_and_refresh_tokens_are_stored_hashed(client, app, central):
    got = connect(client, app, central)
    with central() as db:
        hashes = set(db.scalars(sa.select(OAuthToken.token_hash)))
        assert hash_token(got["access_token"]) in hashes
        assert hash_token(got["refresh_token"]) in hashes
        assert got["access_token"] not in hashes and got["refresh_token"] not in hashes
        for row in db.execute(sa.select(OAuthToken.__table__)).all():
            assert all(got["access_token"] not in str(v) for v in row)


def test_deny_sends_the_client_access_denied(client, app, central):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge, state="s1"))
    got = approve(sign_in(app, central, "alice"), req, yes=False)
    assert got["base"] == REDIRECT and got["error"] == "access_denied" and got["state"] == "s1"
    assert "code" not in got


# --- PKCE ---------------------------------------------------------------------------

def test_a_wrong_code_verifier_is_refused(client, app, central):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    got = approve(sign_in(app, central, "alice"), req)
    bad = exchange(client, client_id, got["code"], "not-the-verifier-" + "x" * 40)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("method", ["plain", "S512"])
def test_only_s256_is_accepted(client, method):
    client_id = register(client).json()["client_id"]
    verifier, _ = pkce()
    r = authorize(client, client_id, verifier, method=method)
    assert r.status_code in (302, 400)
    if r.status_code == 302:  # the error goes back to the registered redirect URI
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_request"]
        assert not r.headers["location"].startswith(f"{BASE_URL}/oauth/consent")


def test_a_missing_code_challenge_is_refused(client):
    client_id = register(client).json()["client_id"]
    r = authorize(client, client_id, None, drop=("code_challenge",))
    assert not r.headers.get("location", "").startswith(f"{BASE_URL}/oauth/consent")


# --- redirect URIs ------------------------------------------------------------------

@pytest.mark.parametrize("uri", [
    "http://evil.example/cb", "javascript:alert(1)", "cursor://anysphere/cb",
    "https://claude.ai/cb#frag", "https://user:pw@claude.ai/cb", "/relative/cb",
    "ftp://claude.ai/cb",
])
def test_registration_refuses_unsafe_redirect_uris(client, uri):
    r = register(client, redirect_uris=[uri])
    assert r.status_code == 400
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


@pytest.mark.parametrize("uri", ["https://claude.ai/api/mcp/auth_callback",
                                 "http://localhost:33418/callback",
                                 "http://127.0.0.1:6274/oauth/callback",
                                 "http://[::1]:8080/cb"])
def test_https_and_loopback_redirect_uris_are_fine(uri):
    assert redirect_uri_problem(uri) is None


def test_authorize_with_an_unregistered_redirect_uri_redirects_nowhere(client):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    for other in ("https://claude.ai/api/mcp/auth_callback/", "https://evil.example/cb",
                  "https://claude.ai/api/mcp/auth_callback?x=1"):
        r = authorize(client, client_id, challenge, redirect=other)
        assert r.status_code == 400, other  # an error page, never a redirect to it
        assert "location" not in r.headers


def test_authorize_requires_an_explicit_redirect_uri(client):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    r = authorize(client, client_id, challenge, drop=("redirect_uri",))
    assert not r.headers.get("location", "").startswith(f"{BASE_URL}/oauth/consent")


def test_a_token_request_with_another_redirect_uri_is_refused(client, app, central):
    reg = register(client, redirect_uris=[REDIRECT, "https://claude.ai/other"]).json()
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    got = approve(sign_in(app, central, "alice"), req)
    bad = exchange(client, reg["client_id"], got["code"], verifier,
                   redirect="https://claude.ai/other")
    assert bad.status_code == 400


def test_a_resource_for_another_server_is_refused(client):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    r = authorize(client, client_id, challenge, resource="https://other.example/mcp")
    assert "invalid_target" in r.headers.get("location", "")


# --- codes --------------------------------------------------------------------------

def test_a_code_works_once_and_reuse_revokes_what_it_issued(client, app, central):
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    got = approve(sign_in(app, central, "alice"), req)
    first = exchange(client, client_id, got["code"], verifier)
    assert first.status_code == 200
    second = exchange(client, client_id, got["code"], verifier)
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"
    access = first.json()["access_token"]
    assert rpc(client, access, "tools/list").status_code == 401
    assert refresh(client, client_id, first.json()["refresh_token"]).status_code == 400


def test_an_expired_code_is_refused(client, app, central, clock):
    client_id = register(client).json()["client_id"]
    verifier, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    got = approve(sign_in(app, central, "alice"), req)
    clock.now += timedelta(minutes=6)
    bad = exchange(client, client_id, got["code"], verifier)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"


def test_a_code_is_bound_to_its_client(client, app, central):
    mine = register(client).json()["client_id"]
    theirs = register(client).json()["client_id"]
    verifier, challenge = pkce()
    req = consent_req(authorize(client, mine, challenge))
    got = approve(sign_in(app, central, "alice"), req)
    bad = exchange(client, theirs, got["code"], verifier)
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    assert exchange(client, mine, got["code"], verifier).status_code == 200


# --- tokens -------------------------------------------------------------------------

def test_an_expired_access_token_is_401_and_refresh_still_works(client, app, central, clock):
    got = connect(client, app, central)
    clock.now += timedelta(hours=1, minutes=1)
    assert rpc(client, got["access_token"], "tools/list").status_code == 401
    again = refresh(client, got["client_id"], got["refresh_token"])
    assert again.status_code == 200
    assert call(client, again.json()["access_token"], "library")["books"]


def test_an_expired_refresh_token_is_refused(client, app, central, clock):
    got = connect(client, app, central)
    clock.now += timedelta(days=31)
    assert refresh(client, got["client_id"], got["refresh_token"]).status_code == 400


def test_a_reused_refresh_token_revokes_the_grant(client, app, central):
    got = connect(client, app, central)
    fresh = refresh(client, got["client_id"], got["refresh_token"]).json()
    assert refresh(client, got["client_id"], got["refresh_token"]).status_code == 400
    # The stolen-copy signal ends the whole connection, the new tokens included.
    assert rpc(client, fresh["access_token"], "tools/list").status_code == 401
    assert refresh(client, got["client_id"], fresh["refresh_token"]).status_code == 400


def test_a_refresh_token_is_bound_to_its_client(client, app, central):
    got = connect(client, app, central)
    other = register(client).json()["client_id"]
    assert refresh(client, other, got["refresh_token"]).status_code == 400
    assert refresh(client, got["client_id"], got["refresh_token"]).status_code == 200


def test_an_access_token_is_not_a_refresh_token_and_vice_versa(client, app, central):
    got = connect(client, app, central)
    assert refresh(client, got["client_id"], got["access_token"]).status_code == 400
    assert rpc(client, got["refresh_token"], "tools/list").status_code == 401


# --- users --------------------------------------------------------------------------

def test_each_user_s_oauth_token_reads_only_their_library(client, app, central):
    alice = connect(client, app, central, "alice")
    bob = connect(client, app, central, "bob")
    assert [b["title"] for b in call(client, alice["access_token"], "library")["books"]] == \
        ["A Study in Scarlet"]
    assert [b["title"] for b in call(client, bob["access_token"], "library")["books"]] == \
        ["Dracula"]
    assert code(call(client, alice["access_token"], "entity", name="Count Dracula")) == "not_found"
    assert code(call(client, bob["access_token"], "entity", name="Holmes")) == "not_found"


def test_one_user_cannot_see_or_revoke_another_s_connections(client, app, central):
    got = connect(client, app, central, "alice")
    alice, bob = sign_in(app, central, "alice"), sign_in(app, central, "bob")
    grant_id = alice.get("/api/me/mcp-connections").json()["connections"][0]["id"]
    assert bob.get("/api/me/mcp-connections").json()["connections"] == []
    assert bob.delete(f"/api/me/mcp-connections/{grant_id}").status_code == 404
    assert rpc(client, got["access_token"], "tools/list").status_code == 200
    assert alice.delete(f"/api/me/mcp-connections/{grant_id}").json() == {"revoked": True}
    assert rpc(client, got["access_token"], "tools/list").status_code == 401
    assert refresh(client, got["client_id"], got["refresh_token"]).status_code == 400


def test_the_code_belongs_to_whoever_approved(client, app, central):
    """A consent link opened by bob gives bob's library, never the requester's."""
    got = connect(client, app, central, "bob")
    assert [b["title"] for b in call(client, got["access_token"], "library")["books"]] == \
        ["Dracula"]


def test_a_disabled_user_s_oauth_tokens_stop_working(client, app, central):
    from lore.central.models import User
    got = connect(client, app, central, "alice")
    with central() as db:
        db.get(User, "alice").status = "disabled"
        db.commit()
    assert rpc(client, got["access_token"], "tools/list").status_code == 401
    assert refresh(client, got["client_id"], got["refresh_token"]).status_code == 400


def test_account_deletion_removes_every_oauth_row(client, app, central, tmp_path):
    from lore.central.erase import erase_user
    got = connect(client, app, central, "alice")
    erase_user(central, tmp_path / "root", "alice", now=datetime.now(timezone.utc))
    with central() as db:
        for model in (OAuthGrant, OAuthCode, OAuthToken):
            assert db.scalar(sa.select(sa.func.count()).select_from(model)) == 0
    assert rpc(client, got["access_token"], "tools/list").status_code == 401


# --- the consent page ---------------------------------------------------------------

def test_the_consent_api_needs_a_session_and_csrf(client, app, central):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.get("/api/oauth/consent", params={"req": req}).status_code == 401
    assert anon.post("/api/oauth/consent", json={"req": req, "approve": True}).status_code == 401
    no_csrf = sign_in(app, central, "alice")
    del no_csrf.headers["X-CSRF-Token"]
    assert no_csrf.post("/api/oauth/consent", json={"req": req, "approve": True}).status_code == 403
    with central() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(OAuthCode)) == 0


def test_a_tampered_or_expired_request_is_refused(client, app, central, clock):
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    alice = sign_in(app, central, "alice")
    for bad in (req[:-2] + "xx", "", "nonsense"):
        r = alice.post("/api/oauth/consent", json={"req": bad, "approve": True})
        assert r.status_code == 400
    assert alice.get("/api/oauth/consent", params={"req": req[:-1]}).status_code == 400


def test_a_bearer_token_cannot_approve_consent(client, app, central):
    got = connect(client, app, central, "alice")
    client_id = register(client).json()["client_id"]
    _, challenge = pkce()
    req = consent_req(authorize(client, client_id, challenge))
    r = TestClient(app, base_url=BASE_URL).post(
        "/api/oauth/consent", json={"req": req, "approve": True},
        headers={"Authorization": f"Bearer {got['access_token']}"})
    assert r.status_code == 401


@pytest.fixture
def mailed(root, central, clock):
    mailer = FakeMailer()
    return make_app(root, central, oauth_clock=clock, mailer=mailer), mailer


def test_the_consent_page_sends_a_signed_out_reader_to_sign_in_and_back(mailed):
    app, mailer = mailed
    with TestClient(app, base_url=BASE_URL) as browser:
        client_id = register(browser).json()["client_id"]
        _, challenge = pkce()
        page = authorize(browser, client_id, challenge).headers["location"]
        path = page[len(BASE_URL):]
        r = browser.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/signin"
        assert "lore_next=" in r.headers["set-cookie"]
        # ...signs in with a magic link, and lands back on the consent page.
        browser.post("/auth/email", json={"email": "alice@example.org"})
        browser.follow_redirects = False
        done = open_magic_link(browser, mailer.links()[-1])
        assert done.status_code == 303, done.text
        assert done.headers["location"] == path
        assert any(h.startswith("lore_next=;") for h in done.headers.get_list("set-cookie"))
        assert browser.get(path, follow_redirects=False).status_code == 200


def test_signing_in_without_a_remembered_page_still_goes_to_the_app(mailed):
    app, mailer = mailed
    with TestClient(app, base_url=BASE_URL) as browser:
        browser.post("/auth/email", json={"email": "alice@example.org"})
        browser.follow_redirects = False
        done = open_magic_link(browser, mailer.links()[-1])
    assert done.headers["location"] == "/app"


def test_only_the_consent_page_can_be_remembered():
    from lore.auth import next as next_page
    for bad in ("/app", "https://evil.example/x", "//evil.example", "/oauth/consent?req=a//b",
                "/oauth/consentx?req=a", "/oauth/consent?req=a#x"):
        assert next_page.allowed(bad) is False
    assert next_page.allowed("/oauth/consent?req=abc.def-_") is True


def test_registration_is_rate_limited_per_address(client, app):
    from lore.mcp.hosted import REGISTER_PER_HOUR
    codes = [register(client).status_code for _ in range(REGISTER_PER_HOUR + 1)]
    assert codes[:-1] == [201] * REGISTER_PER_HOUR and codes[-1] == 429


def test_settings_say_oauth_is_on(client, app, central):
    assert sign_in(app, central, "alice").get("/api/mcp").json()["oauth"] is True


def test_oauth_is_off_without_an_https_origin(root, central):
    from tests.auth_fixtures import make_settings
    app = make_app(root, central, auth=make_settings(base_url="http://lore.example.org"))
    with TestClient(app, base_url="http://lore.example.org") as c:
        assert c.get("/.well-known/oauth-authorization-server").status_code == 404
        assert c.post("/register", json={}).status_code in (404, 405)
