"""Keys for Gemini, Claude and OpenAI per user (central DB, migration 0010,
KeyService and the /api/me provider routes). The key checks are faked; no
network."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import anthropic
import httpx2
import openai
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central import db as central
from lore.central import keys as K
from lore.central.models import ApiKey, User
from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.key_scope import KeyRequired
from tests.auth_fixtures import FAKE_KEY, FakeKeyChecker, make_app, sign_in
from tests.central_fixtures import central_sessions

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CLAUDE_KEY = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfakeFAKEfake-AAAA"
OPENAI_KEY = "sk-proj-FAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEBBBB"
OPENAI_MODELS = ["gpt-9", "gpt-9-mini", "gpt-9-nano", "text-embedding-4", "tts-2"]


class Checker:
    def __init__(self, result=K.CheckResult(K.CHECK_OK, [])):
        self.result, self.seen = result, []

    def check(self, key):
        self.seen.append(key)
        return self.result


def service(sessions, now=NOW, **checkers):
    return K.KeyService(sessions, K.MasterKeys(bytes([1]) * 32), Checker(K.CHECK_OK),
                        clock=lambda: now, checkers=checkers or {
                            "anthropic": Checker(K.CheckResult(
                                K.CHECK_OK, ["claude-sonnet-5-5", "claude-opus-5-5",
                                             "claude-haiku-5-5"])),
                            "openai": Checker(K.CheckResult(
                                K.CHECK_OK, ["gpt-9", "gpt-9-mini", "gpt-9-nano"]))})


@pytest.fixture
def sessions(tmp_path):
    s, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return s


# --- migration 0010 ----------------------------------------------------------------

def test_existing_gemini_rows_survive_the_migration(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    central.upgrade(url, "0009")
    engine = central.make_engine(url)
    mk = K.MasterKeys(bytes([1]) * 32)
    sealed = K.seal(mk, "alice", FAKE_KEY)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, role, status, created_at)"
                             " VALUES ('alice', 'a@x.org', 'user', 'active', :t)"),
                     {"t": NOW.replace(tzinfo=None)})
        conn.execute(sa.text(
            "INSERT INTO api_keys (user_id, provider, ciphertext, nonce, key_version,"
            " last4, added_at, last_ok_at, last_error) VALUES ('alice', 'gemini', :c, :n,"
            " :v, 'e123', :t, :t, NULL)"),
            {"c": sealed.ciphertext, "n": sealed.nonce, "v": sealed.key_version,
             "t": NOW.replace(tzinfo=None)})
        conn.execute(sa.text("INSERT INTO usage (user_id, day, model, requests)"
                             " VALUES ('alice', '2026-11-07', 'gemini-3.1-flash-lite', 4)"))
    engine.dispose()
    central.upgrade(url)
    sessions = central.make_session_factory(central.make_engine(url))
    with sessions() as db:
        row = db.get(ApiKey, ("alice", "gemini"))
        assert (row.ciphertext, row.nonce, row.last4) == (sealed.ciphertext, sealed.nonce,
                                                          "e123")
        assert row.model is None and row.models is None
        assert db.get(User, "alice").active_provider == "gemini"
        assert db.get(User, "alice").daily_token_cap is None
        usage = db.execute(sa.text("SELECT provider, requests, input_tokens FROM usage"))
        assert usage.one() == ("gemini", 4, 0)
    svc = K.KeyService(sessions, mk, Checker(K.CHECK_OK))
    assert svc.key_for("alice") == FAKE_KEY  # the same seal still opens
    # and a second provider row is now possible
    svc2 = service(sessions)
    assert svc2.save("alice", CLAUDE_KEY, "anthropic").stored


def test_downgrade_keeps_gemini_rows_only(tmp_path):
    from alembic import command
    from lore.central.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'c.db'}"
    sessions, url = central_sessions(tmp_path, users=("alice",))
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.save("alice", CLAUDE_KEY, "anthropic")
    command.downgrade(alembic_config(url), "0009")
    engine = central.make_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT user_id, provider FROM api_keys")).all()
    assert rows == [("alice", "gemini")]


# --- KeyService --------------------------------------------------------------------

def test_one_key_per_provider_each_sealed_for_its_provider(sessions):
    svc = service(sessions)
    assert svc.save("alice", FAKE_KEY).stored
    out = svc.save("alice", CLAUDE_KEY, "anthropic")
    assert out.stored and "claude-sonnet-5-5" in out.models
    with sessions() as db:
        rows = {r.provider: r for r in db.scalars(sa.select(ApiKey))}
    assert set(rows) == {"gemini", "anthropic"}
    assert CLAUDE_KEY.encode() not in rows["anthropic"].ciphertext
    assert rows["anthropic"].model == "claude-sonnet-5-5"  # the suggested default
    # the Claude seal opens only as Claude's (the provider is associated data)
    sealed = K.Sealed(rows["anthropic"].ciphertext, rows["anthropic"].nonce,
                      rows["anthropic"].key_version)
    with pytest.raises(K.KeyDecryptError):
        K.unseal(K.MasterKeys(bytes([1]) * 32), "alice", sealed, "gemini")
    # Gemini stays active: it had a key first
    assert svc.active_provider("alice") == "gemini"
    assert svc.credentials_for("alice").provider == "gemini"


def test_the_first_key_becomes_active_and_switching_needs_a_key(sessions):
    svc = service(sessions)
    svc.save("bob", OPENAI_KEY, "openai")
    assert svc.active_provider("bob") == "openai"
    creds = svc.credentials_for("bob")
    assert (creds.provider, creds.key, creds.model) == ("openai", OPENAI_KEY, "gpt-9-nano")
    with pytest.raises(K.ProviderChoiceError):
        svc.choose("bob", "anthropic", "claude-opus-5-5")  # not connected
    with pytest.raises(K.ProviderChoiceError):
        svc.choose("bob", "openai", "gpt-unknown")
    svc.choose("bob", "openai", "gpt-9", fast_model="gpt-9-nano")
    creds = svc.credentials_for("bob")
    assert (creds.model, creds.fast_model) == ("gpt-9", "gpt-9-nano")
    svc.save("bob", CLAUDE_KEY, "anthropic")
    assert svc.active_provider("bob") == "openai"
    svc.choose("bob", "anthropic", "claude-opus-5-5")
    assert svc.credentials_for("bob").model == "claude-opus-5-5"
    with pytest.raises(K.ProviderChoiceError):
        svc.choose("bob", "anthropic", "claude-sonnet-4-6")


def test_removing_the_active_key_moves_to_another_connected_one(sessions):
    svc = service(sessions)
    svc.save("alice", CLAUDE_KEY, "anthropic")
    svc.save("alice", OPENAI_KEY, "openai")
    assert svc.active_provider("alice") == "anthropic"
    assert svc.remove("alice", "anthropic")
    assert svc.active_provider("alice") == "openai"
    assert svc.remove("alice")  # the active one
    with pytest.raises(KeyRequired):
        svc.credentials_for("alice")


def test_formats_are_checked_per_provider_before_any_request(sessions):
    claude = Checker(K.CheckResult(K.CHECK_OK, []))
    svc = service(sessions, anthropic=claude, openai=Checker())
    for text, provider in ((FAKE_KEY, "anthropic"), (CLAUDE_KEY[:12], "anthropic"),
                           ("not a key", "openai"), (CLAUDE_KEY, "nowhere")):
        with pytest.raises(K.KeyFormatError):
            svc.save("alice", text, provider)
    assert claude.seen == []


def test_an_invalid_key_is_not_stored_and_an_unchecked_one_is(sessions):
    svc = service(sessions, anthropic=Checker(K.CheckResult(K.CHECK_INVALID, [])),
                  openai=Checker(K.CheckResult(K.CHECK_UNAVAILABLE, [])))
    assert not svc.save("alice", CLAUDE_KEY, "anthropic").stored
    out = svc.save("alice", OPENAI_KEY, "openai")
    assert out.stored and svc.status("alice", "openai")["last_error"] == K.ERR_UNCHECKED


def test_the_scope_carries_provider_model_usage_and_cap(sessions):
    svc = service(sessions)
    svc.save("alice", CLAUDE_KEY, "anthropic")
    svc.choose("alice", "anthropic", "claude-opus-5-5", fast_model="claude-haiku-5-5")
    scope = svc.scope("alice")
    assert (scope.provider, scope.key, scope.model, scope.fast_model) == (
        "anthropic", CLAUDE_KEY, "claude-opus-5-5", "claude-haiku-5-5")
    assert CLAUDE_KEY not in repr(scope)
    with key_scope.bound(scope):
        key_scope.note_request("claude-opus-5-5")
        key_scope.note_usage("anthropic", "claude-opus-5-5", 1000, 200)
        key_scope.note_usage("anthropic", "claude-haiku-5-5", 500, 100)
        assert not key_scope.over_token_cap()
        svc.set_token_cap("alice", 1500)
        assert key_scope.over_token_cap()
    summary = svc.usage_summary("alice")
    assert summary["today"]["input_tokens"] == 1500
    assert summary["today"]["requests"] == 1
    opus = summary["today"]["models"]["claude-opus-5-5"]
    assert opus["estimated_cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    assert summary["cap_reached"] and summary["daily_token_cap"] == 1500
    assert summary["month_total"]["output_tokens"] == 300
    svc.set_token_cap("alice", None)
    assert not svc.over_cap("alice")


def test_openai_usage_has_tokens_but_no_invented_price(sessions):
    svc = service(sessions)
    svc.record_tokens("bob", "openai", "gpt-9-nano", 100, 10)
    today = svc.usage_summary("bob")["today"]
    assert today["input_tokens"] == 100 and today["estimated_cost_usd"] is None


# --- the real checkers, against faked SDK clients -----------------------------------

def _status(cls, code):
    request = httpx2.Request("GET", "https://example.invalid/v1/models")
    return cls("x", response=httpx2.Response(code, request=request), body=None)


def test_the_claude_checker_lists_models_and_filters_to_lores_three():
    listed = [NS(id="claude-sonnet-5-5"), NS(id="claude-haiku-5-5"), NS(id="claude-x")]
    ok = K.AnthropicKeyChecker(lambda key: NS(models=NS(list=lambda **kw: listed)))
    assert ok.check(CLAUDE_KEY) == (K.CHECK_OK, ["claude-sonnet-5-5", "claude-haiku-5-5"])

    def raising(exc):
        def boom(**kw):
            raise exc
        return K.AnthropicKeyChecker(lambda key: NS(models=NS(list=boom)))

    assert raising(_status(anthropic.AuthenticationError, 401)).check(CLAUDE_KEY).status \
        == K.CHECK_INVALID
    assert raising(_status(anthropic.PermissionDeniedError, 403)).check(CLAUDE_KEY).status \
        == K.CHECK_INVALID
    assert raising(_status(anthropic.RateLimitError, 429)).check(CLAUDE_KEY).status \
        == K.CHECK_UNAVAILABLE
    assert raising(RuntimeError("no network")).check(CLAUDE_KEY).status \
        == K.CHECK_UNAVAILABLE


def test_the_openai_checker_offers_chat_models_only():
    listed = [NS(id=i) for i in OPENAI_MODELS]
    ok = K.OpenAIKeyChecker(lambda key: NS(models=NS(list=lambda: listed)))
    assert ok.check(OPENAI_KEY) == (K.CHECK_OK, ["gpt-9", "gpt-9-mini", "gpt-9-nano"])

    def boom():
        raise _status(openai.AuthenticationError, 401)
    bad = K.OpenAIKeyChecker(lambda key: NS(models=NS(list=boom)))
    assert bad.check(OPENAI_KEY).status == K.CHECK_INVALID


def test_the_real_checkers_build_sdk_clients_without_a_request():
    assert isinstance(K.AnthropicKeyChecker()._client(CLAUDE_KEY), anthropic.Anthropic)
    assert isinstance(K.OpenAIKeyChecker()._client(OPENAI_KEY), openai.OpenAI)


# --- the API -----------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(K.AnthropicKeyChecker, "check", lambda self, key: K.CheckResult(
        K.CHECK_INVALID if key.endswith("XXXX") else K.CHECK_OK,
        ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]))
    monkeypatch.setattr(K.OpenAIKeyChecker, "check", lambda self, key: K.CheckResult(
        K.CHECK_OK, ["gpt-9", "gpt-9-mini"]))
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions,
                   key_checker=FakeKeyChecker())
    return sign_in(app, sessions, "alice")


def test_providers_api_connect_switch_and_remove(client):
    r = client.get("/api/me/providers")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [p["provider"] for p in r.json()["providers"]] == ["gemini", "anthropic", "openai"]
    assert not any(p["connected"] for p in r.json()["providers"])

    r = client.put("/api/me/key", json={"provider": "anthropic", "key": CLAUDE_KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "anthropic" and body["last4"] == "AAAA"
    assert body["models"] == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
    assert body["active"] == "anthropic"
    assert CLAUDE_KEY not in r.text

    r = client.put("/api/me/key", json={"provider": "openai", "key": OPENAI_KEY})
    assert r.json()["models"] == ["gpt-9", "gpt-9-mini"]
    state = client.get("/api/me/providers").json()
    claude = state["providers"][1]
    assert claude["connected"] and claude["active"] and claude["model"] == "claude-sonnet-5-5"
    assert state["providers"][2]["model"] == "gpt-9-mini"  # the cheapest suggestion

    r = client.put("/api/me/provider", json={"provider": "openai", "model": "gpt-9"})
    assert r.status_code == 200 and r.json()["active"] == "openai"
    r = client.put("/api/me/provider", json={"provider": "gemini"})
    assert r.status_code == 400  # no Gemini key
    r = client.put("/api/me/provider", json={"provider": "anthropic",
                                             "model": "claude-sonnet-4-6"})
    assert r.status_code == 400

    r = client.delete("/api/me/key?provider=openai")
    assert r.json() == {"deleted": True}
    assert client.get("/api/me/providers").json()["active"] == "anthropic"
    assert client.delete("/api/me/key?provider=nowhere").status_code == 400


def test_key_errors_are_friendly_and_name_the_provider(client):
    r = client.put("/api/me/key", json={"provider": "anthropic", "key": FAKE_KEY})
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_format"
    assert FAKE_KEY not in r.text
    bad = CLAUDE_KEY[:-4] + "XXXX"
    r = client.put("/api/me/key", json={"provider": "anthropic", "key": bad})
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_invalid"
    assert bad not in r.text
    r = client.put("/api/me/key", json={"provider": "mistral", "key": CLAUDE_KEY})
    assert r.status_code == 400


def test_usage_and_token_cap_routes(client):
    client.put("/api/me/key", json={"provider": "anthropic", "key": CLAUDE_KEY})
    r = client.put("/api/me/token-cap", json={"cap": 200000})
    assert r.status_code == 200 and r.json()["daily_token_cap"] == 200000
    usage = client.get("/api/me/usage").json()
    assert usage["provider"] == "anthropic"
    assert usage["tokens"]["daily_token_cap"] == 200000
    assert client.put("/api/me/token-cap", json={"cap": -1}).status_code == 400
    assert client.put("/api/me/token-cap", json={"cap": "lots"}).status_code == 400
    assert client.put("/api/me/token-cap", json={"cap": None}).json()["daily_token_cap"] \
        is None


def test_provider_routes_need_the_csrf_token(client):
    token = client.headers.pop("x-csrf-token", None)
    try:
        r = client.put("/api/me/provider", json={"provider": "anthropic"})
        assert r.status_code == 403
    finally:
        if token:
            client.headers["x-csrf-token"] = token
