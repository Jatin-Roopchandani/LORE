"""A paid Gemini plan: the "I'm on a paid Gemini plan" switch (migration 0011,
KeyService, PUT /api/me/provider, [lore.llm] gemini_paid).

On: no free-tier request cap and no answer reserve; tokens recorded from
Gemini's usageMetadata with a cost from roles.GEMINI_PRICES (tokens only where
no price is verified); the daily token cap; the processing estimate and its
confirmation. Off: the free tier exactly as before (also test_gemini_golden.py
and test_gemini_budget.py). All offline."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from lore.central import db as central
from lore.central import keys as K
from lore.central.models import ApiKey, Usage
from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.gemini import GeminiClient, GeminiQuotaError
from lore.inference.key_scope import KeyScope
from lore.inference.providers import estimate as E
from lore.inference.providers import factory, roles
from tests.auth_fixtures import FAKE_KEY, FakeKeyChecker, make_app, sign_in
from tests.central_fixtures import central_sessions

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
DAY = "2026-11-07"  # NOW in Pacific
LITE = "gemini-3.1-flash-lite"
LITE2 = "gemini-3.5-flash-lite"


def service(sessions):
    return K.KeyService(sessions, K.MasterKeys(bytes([1]) * 32), FakeKeyChecker(),
                        clock=lambda: NOW)


@pytest.fixture
def sessions(tmp_path):
    s, _ = central_sessions(tmp_path, users=("alice", "bob"))
    return s


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LORE_MULTI_USER",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _config(tmp_path, **llm):
    cfg = LoreConfig(data_dir=tmp_path / "data", extraction_provider="gemini",
                     selection={"provider": "gemini"}, synthesis={"provider": "gemini"},
                     adjudication={"provider": "gemini"}, llm=llm or {})
    cfg.ensure_dirs()
    return cfg


def _spend_everything(cfg):
    from lore.db.sqlite import open_db
    from lore.orchestrator.gemini_budget import record_call, today
    conn = open_db(cfg.sqlite_path)
    models = dict.fromkeys([*cfg.synthesis.models, *cfg.extraction.gemini_models])
    for model in models:
        for _ in range(500):
            record_call(conn, model, today())
    return conn


# --- the price table ---------------------------------------------------------------

def test_gemini_prices_are_the_verified_ones_and_unknown_models_have_none():
    assert roles.gemini_price(LITE, DAY) == (0.25, 1.50)
    assert roles.gemini_price(LITE2, DAY) == (0.30, 2.50)
    assert roles.gemini_price("gemini-3.8-flash", "2026-12-31") == (0.75, 3.75)
    assert roles.gemini_price("gemini-3.8-flash", "2027-01-01") == (1.50, 7.50)
    assert roles.gemini_price("gemini-3.7-flash", DAY) is None  # not on Google's page
    assert roles.gemini_cost("gemini-3.7-flash", 10, 10, DAY) is None
    assert roles.gemini_cost(LITE, 1_000_000, 1_000_000, DAY) == pytest.approx(1.75)


# --- migration 0011 ----------------------------------------------------------------

def test_migration_0011_up_keeps_every_key_free_and_down_drops_the_columns(tmp_path):
    from alembic import command
    from lore.central.migrate import alembic_config

    url = f"sqlite:///{tmp_path / 'c.db'}"
    central.upgrade(url, "0010")
    engine = central.make_engine(url)
    mk = K.MasterKeys(bytes([1]) * 32)
    sealed = K.seal(mk, "alice", FAKE_KEY)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email, role, status, created_at)"
                             " VALUES ('alice', 'a@x.org', 'user', 'active', :t)"),
                     {"t": NOW.replace(tzinfo=None)})
        conn.execute(sa.text(
            "INSERT INTO api_keys (user_id, provider, ciphertext, nonce, key_version,"
            " last4, added_at) VALUES ('alice', 'gemini', :c, :n, :v, 'e123', :t)"),
            {"c": sealed.ciphertext, "n": sealed.nonce, "v": sealed.key_version,
             "t": NOW.replace(tzinfo=None)})
        conn.execute(sa.text("INSERT INTO usage (user_id, day, model, requests)"
                             f" VALUES ('alice', '{DAY}', '{LITE}', 4)"))
    engine.dispose()
    central.upgrade(url)
    sessions = central.make_session_factory(central.make_engine(url))
    with sessions() as db:
        assert db.get(ApiKey, ("alice", "gemini")).paid_tier is False
        assert db.get(Usage, ("alice", DAY, LITE)).quota_refusals == 0
    svc = K.KeyService(sessions, mk, FakeKeyChecker(), clock=lambda: NOW)
    assert svc.key_for("alice") == FAKE_KEY
    svc.set_gemini_paid("alice", True)

    command.downgrade(alembic_config(url), "0010")
    engine = central.make_engine(url)
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("api_keys")}
        usage_cols = {c["name"] for c in sa.inspect(conn).get_columns("usage")}
        assert conn.execute(sa.text("SELECT user_id, provider FROM api_keys")).all() == [
            ("alice", "gemini")]
        assert conn.execute(sa.text("SELECT requests FROM usage")).scalar_one() == 4
    assert "paid_tier" not in cols and "quota_refusals" not in usage_cols
    engine.dispose()
    central.upgrade(url)  # and up again


# --- GeminiClient: tokens and 429s reach the bound scope --------------------------

class G(BaseModel):
    ok: bool


def _client(handler):
    client = GeminiClient("AIzaTESTtestTESTtestTESTtestTESTtest0001", models=[LITE, LITE2],
                          transport=httpx.MockTransport(handler))

    async def no_wait(model):
        return None
    client._throttle = no_wait
    return client


def test_the_client_reports_usage_metadata_and_429s():
    seen, refused = [], []

    def handler(request):
        if LITE in str(request.url):
            return httpx.Response(429, json={"error": {}})
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30,
                              "thoughtsTokenCount": 5, "totalTokenCount": 155}})

    scope = KeyScope("alice", "k", on_usage=lambda *a: seen.append(a),
                     on_refusal=refused.append)

    async def go():
        client = _client(handler)
        try:
            return await client.generate_json("q", G)
        finally:
            await client.aclose()

    with key_scope.bound(scope):
        assert asyncio.run(go()).ok
    assert refused == [LITE]
    assert seen == [("gemini", LITE2, 120, 35)]  # thinking is billed as output


def test_no_usage_metadata_reports_nothing():
    seen = []

    def handler(request):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "yes"}]}}]})

    async def go():
        client = _client(handler)
        try:
            return await client.generate("q")
        finally:
            await client.aclose()

    with key_scope.bound(KeyScope("alice", "k", on_usage=lambda *a: seen.append(a))):
        assert asyncio.run(go()) == "yes"
    assert seen == []


# --- KeyService ---------------------------------------------------------------------

def test_the_switch_needs_a_gemini_key_and_is_per_user(sessions):
    svc = service(sessions)
    with pytest.raises(K.ProviderChoiceError):
        svc.set_gemini_paid("alice", True)
    svc.save("alice", FAKE_KEY)
    svc.save("bob", FAKE_KEY)
    state = svc.set_gemini_paid("alice", True)
    assert state["gemini_paid"] is True and state["providers"][0]["paid_tier"] is True
    assert svc.providers("bob")["gemini_paid"] is False
    assert svc.credentials_for("alice").paid_tier is True
    assert svc.credentials_for("bob").paid_tier is False
    with pytest.raises(K.ProviderChoiceError):
        svc.set_gemini_paid("alice", "yes")
    svc.set_gemini_paid("alice", False)
    assert svc.providers("alice")["gemini_paid"] is False


def test_paid_on_records_tokens_with_a_cost_and_the_cap_applies(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.set_gemini_paid("alice", True)
    scope = svc.scope("alice")
    assert scope.paid_tier
    with key_scope.bound(scope):
        key_scope.note_request(LITE)
        key_scope.note_usage("gemini", LITE, 100_000, 20_000)
        key_scope.note_usage("gemini", "gemini-3.7-flash", 50, 10)
        assert not key_scope.over_token_cap()
        svc.set_token_cap("alice", 100_000)
        assert key_scope.over_token_cap()
    today = svc.usage_summary("alice")["today"]
    assert today["input_tokens"] == 100_050 and today["output_tokens"] == 20_010
    assert today["models"][LITE]["estimated_cost_usd"] == pytest.approx(
        (100_000 * 0.25 + 20_000 * 1.50) / 1e6)
    assert today["models"]["gemini-3.7-flash"]["estimated_cost_usd"] is None  # tokens only
    assert today["estimated_cost_usd"] == pytest.approx(0.055)
    assert svc.usage_today("alice")["gemini_paid"] is True


def test_paid_off_records_requests_only_exactly_as_before(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    scope = svc.scope("alice")
    assert not scope.paid_tier
    with key_scope.bound(scope):
        key_scope.note_request(LITE)
        key_scope.note_usage("gemini", LITE, 1000, 200)  # ignored on the free tier
    today = svc.usage_summary("alice")["today"]
    assert today["requests"] == 1
    assert today["input_tokens"] == 0 and today["estimated_cost_usd"] is None
    assert today["models"][LITE]["estimated_cost_usd"] is None
    usage = svc.usage_today("alice")
    assert usage["requests"] == 1 and usage["limit"] == K.DAILY_ALLOWANCE
    assert usage["gemini_paid"] is False


# --- the hint ---------------------------------------------------------------------

def _requests(svc, user, model, n):
    for _ in range(n):
        svc.record_request(user, model, "gemini")


def test_the_hint_only_past_the_free_count_with_no_429(sessions):
    svc = service(sessions)
    svc.save("alice", FAKE_KEY)
    svc.save("bob", FAKE_KEY)
    _requests(svc, "alice", LITE, 500)
    assert svc.providers("alice")["gemini_paid_hint"] is False  # at the count, not past it
    _requests(svc, "alice", LITE, 1)
    assert svc.providers("alice")["gemini_paid_hint"] is True
    assert svc.providers("bob")["gemini_paid_hint"] is False  # per user
    svc.set_gemini_paid("alice", True)
    assert svc.providers("alice")["gemini_paid_hint"] is False  # already on
    svc.set_gemini_paid("alice", False)
    assert svc.providers("alice")["gemini_paid_hint"] is True  # never switched for them
    with key_scope.bound(svc.scope("alice")):
        key_scope.note_quota_refusal(LITE2)
    assert svc.providers("alice")["gemini_paid_hint"] is False  # Google said 429


def test_no_hint_without_a_gemini_key(sessions):
    svc = service(sessions)
    _requests(svc, "alice", LITE, 600)
    assert svc.providers("alice")["gemini_paid_hint"] is False


# --- the budgets ----------------------------------------------------------------------

def test_paid_gemini_has_no_request_cap_but_the_token_cap(clean_env, monkeypatch):
    from lore.mcp.tools import _check_synthesis_budget

    cfg = _config(clean_env)
    _spend_everything(cfg).close()
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSERVERenvSERVERenvSERVERenvSERVE6789")
    assert _check_synthesis_budget(cfg) is not None  # free tier: spent
    capped = [False]
    paid = KeyScope("alice", FAKE_KEY, paid_tier=True, over_cap=lambda: capped[0])
    with key_scope.bound(paid):
        assert not factory.on_gemini(cfg)
        assert _check_synthesis_budget(cfg) is None
        capped[0] = True
        assert "token cap" in _check_synthesis_budget(cfg)
    with key_scope.bound(KeyScope("bob", FAKE_KEY, over_cap=lambda: True)):
        assert factory.on_gemini(cfg)
        assert _check_synthesis_budget(cfg) is not None  # free: the ledger, not the cap


def test_paid_gemini_processing_has_no_reserve_but_the_token_cap(clean_env):
    from lore.orchestrator.pipeline import JobCtl, QuotaStop, StageContext

    cfg = _config(clean_env)
    conn = _spend_everything(cfg)
    ctl = JobCtl(StageContext(conn, cfg, "book"))
    with pytest.raises(QuotaStop, match="reserve"):
        ctl.spend_check()
    capped = [False]
    with key_scope.bound(KeyScope("a", FAKE_KEY, paid_tier=True,
                                  over_cap=lambda: capped[0])):
        ctl.spend_check()
        capped[0] = True
        with pytest.raises(QuotaStop, match="token cap"):
            ctl.spend_check()
    conn.close()


def test_local_mode_gemini_paid_setting(clean_env, monkeypatch):
    from lore.mcp.tools import _check_synthesis_budget
    from lore.orchestrator.pipeline import JobCtl, StageContext

    cfg = _config(clean_env, gemini_paid=True)
    conn = _spend_everything(cfg)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSERVERenvSERVERenvSERVERenvSERVE6789")
    assert factory.gemini_paid(cfg) and not factory.on_gemini(cfg)
    assert _check_synthesis_budget(cfg) is None
    JobCtl(StageContext(conn, cfg, "book")).spend_check()  # no reserve
    conn.close()
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    assert not factory.gemini_paid(cfg)  # multi mode: only the user's own switch
    assert not factory.gemini_paid(_config(clean_env / "free"))


def test_a_paid_scope_on_another_provider_is_not_gemini_paid(clean_env):
    cfg = _config(clean_env)
    with key_scope.bound(KeyScope("a", "sk-ant-x", provider="anthropic", paid_tier=True)):
        assert not factory.gemini_paid(cfg)


# --- the estimate --------------------------------------------------------------------

def test_the_estimate_on_a_paid_gemini_plan():
    free = E.estimate(5000, "gemini", None)
    assert free == E.estimate(5000, "gemini", None, gemini_paid=False)
    assert not free["needs_confirm"] and free["estimated_cost_usd"] is None
    paid = E.estimate(5000, "gemini", LITE, gemini_paid=True)
    expected = (paid["input_tokens"] * 0.25 + paid["output_tokens"] * 1.50) / 1e6
    assert paid["estimated_cost_usd"] == pytest.approx(round(expected, 2))
    assert paid["needs_confirm"]  # above $1
    assert not E.estimate(10, "gemini", LITE, gemini_paid=True)["needs_confirm"]
    unpriced = E.estimate(10, "gemini", "gemini-3.7-flash", gemini_paid=True)
    assert unpriced["estimated_cost_usd"] is None and not unpriced["needs_confirm"]
    assert E.estimate(5000, "gemini", "gemini-3.7-flash",
                      gemini_paid=True)["needs_confirm"]  # by tokens, as for OpenAI


# --- the API ------------------------------------------------------------------------

@pytest.fixture
def app_and_sessions(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice", "bob"))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions,
                   key_checker=FakeKeyChecker())
    return app, sessions


def test_the_switch_api_auth_csrf_and_isolation(app_and_sessions):
    from fastapi.testclient import TestClient
    from tests.auth_fixtures import BASE_URL

    app, sessions = app_and_sessions
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.put("/api/me/provider", json={"gemini_paid": True}).status_code in (401, 403)

    alice = sign_in(app, sessions, "alice")
    r = alice.put("/api/me/provider", json={"gemini_paid": True})
    assert r.status_code == 400  # no Gemini key yet
    assert alice.put("/api/me/key", json={"key": FAKE_KEY}).status_code == 200
    assert alice.get("/api/me/providers").json()["gemini_paid"] is False

    token = alice.headers.pop("x-csrf-token")
    assert alice.put("/api/me/provider", json={"gemini_paid": True}).status_code == 403
    alice.headers["x-csrf-token"] = token
    assert alice.get("/api/me/providers").json()["gemini_paid"] is False

    assert alice.put("/api/me/provider", json={"gemini_paid": "yes"}).status_code == 400
    r = alice.put("/api/me/provider", json={"gemini_paid": True})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["gemini_paid"] is True and r.json()["active"] == "gemini"
    state = alice.get("/api/me/providers").json()
    assert state["gemini_paid"] is True and state["providers"][0]["paid_tier"] is True
    assert FAKE_KEY not in json.dumps(state)
    assert alice.get("/api/me/usage").json()["gemini_paid"] is True

    bob = sign_in(app, sessions, "bob")
    bob.put("/api/me/key", json={"key": FAKE_KEY})
    assert bob.get("/api/me/providers").json()["gemini_paid"] is False
    assert bob.get("/api/me/usage").json()["gemini_paid"] is False

    r = alice.put("/api/me/provider", json={"gemini_paid": False})
    assert r.json()["gemini_paid"] is False


def test_the_estimate_endpoint_on_and_off(app_and_sessions, tmp_path):
    from lore.db.sqlite import open_db
    from lore.tenancy import config_for

    app, sessions = app_and_sessions
    root = LoreConfig(data_dir=tmp_path / "root")
    conn = open_db(config_for(root, "alice").sqlite_path)
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b1', 'Book', 'x')")
    conn.executemany("INSERT INTO chunks (id, book_id, chapter_seq, scene_seq, seq, text)"
                     " VALUES (?, 'b1', 1, 1, ?, 't')", [(f"c{i}", i) for i in range(5000)])
    conn.commit()
    conn.close()
    alice = sign_in(app, sessions, "alice")
    alice.put("/api/me/key", json={"key": FAKE_KEY})
    free = alice.get("/api/books/b1/estimate").json()
    assert free["model"] is None and free["estimated_cost_usd"] is None
    assert not free["needs_confirm"]
    alice.put("/api/me/provider", json={"gemini_paid": True})
    paid = alice.get("/api/books/b1/estimate").json()
    assert paid["model"] == LITE and paid["estimated_cost_usd"] > 1 and paid["needs_confirm"]
