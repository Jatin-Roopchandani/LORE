"""The Library footer follows the user's provider and mode (/api/books
`usage`), and /api/quota stops reporting a Gemini ledger for a library on a
billed provider in local mode. Offline."""
from fastapi.testclient import TestClient

from lore.central.keys import KeyService
from lore.config import LoreConfig
from lore.web.app import create_app
from tests.auth_fixtures import FAKE_KEY, BASE_URL, Clock, FakeKeyChecker, make_app, master_keys, sign_in
from tests.central_fixtures import central_sessions

CLAUDE_KEY = "sk-ant-api03-ALICEaliceALICEaliceALICEalice-0001"


class Checker:
    def check(self, key):
        from lore.central import keys as K
        return K.CheckResult(K.CHECK_OK, ["claude-sonnet-5-5"])


def _alice(tmp_path):
    sessions, _ = central_sessions(tmp_path, users=("alice",))
    app = make_app(LoreConfig(data_dir=tmp_path / "root"), sessions)
    return sign_in(app, sessions, "alice"), sessions


def test_multi_gemini_free_then_paid_then_claude(tmp_path, monkeypatch):
    from lore.central import keys as K
    monkeypatch.setattr(K.AnthropicKeyChecker, "check", lambda self, key: Checker().check(key))
    alice, sessions = _alice(tmp_path)
    alice.put("/api/me/key", json={"key": FAKE_KEY})
    body = alice.get("/api/books").json()
    assert body["usage"] == {"mode": "free", "provider": "gemini"}
    assert "gemini" in body  # the free tier's numbers, as before

    alice.put("/api/me/provider", json={"gemini_paid": True})
    svc = KeyService(sessions, master_keys(), FakeKeyChecker(), clock=Clock())
    svc.record_tokens("alice", "gemini", "gemini-3.1-flash-lite", 1_000_000, 0)
    usage = alice.get("/api/books").json()["usage"]
    assert usage["mode"] == "metered" and usage["provider"] == "gemini"
    assert usage["tokens_today"] == 1_000_000 and usage["cost_usd"] == 0.25

    alice.put("/api/me/key", json={"provider": "anthropic", "key": CLAUDE_KEY})
    alice.put("/api/me/provider", json={"provider": "anthropic"})
    alice.put("/api/me/token-cap", json={"cap": 5_000_000})
    usage = alice.get("/api/books").json()["usage"]
    assert usage["provider"] == "anthropic" and usage["daily_token_cap"] == 5_000_000


def test_local_mode_on_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    cfg = LoreConfig(data_dir=tmp_path / "data", synthesis={"provider": "gemini"},
                     llm={"provider": "anthropic"})
    cfg.ensure_dirs()
    client = TestClient(create_app(cfg), base_url=BASE_URL)
    usage = client.get("/api/books").json()["usage"]
    assert usage == {"mode": "metered", "provider": "anthropic", "tokens_today": None,
                     "cost_usd": None, "daily_token_cap": None}
    quota = client.get("/api/quota").json()
    assert quota["provider"] == "billed" and quota["billed_by"] == "anthropic"
    assert quota["models"] == []


def test_local_mode_free_gemini_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    cfg = LoreConfig(data_dir=tmp_path / "data", synthesis={"provider": "gemini"})
    cfg.ensure_dirs()
    client = TestClient(create_app(cfg), base_url=BASE_URL)
    assert client.get("/api/books").json()["usage"] == {"mode": "free", "provider": "gemini"}
    assert client.get("/api/quota").json()["provider"] == "gemini"
