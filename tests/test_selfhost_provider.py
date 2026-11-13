"""Self-host mode: the admin's AI provider for everyone, and the boundary
around the local model's address (only the admin sets it; the hosted site
refuses the provider entirely)."""
import json

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from lore.central.keys import KeyService
from lore.central.models import ServerSetting, User
from lore.config import LoreConfig
from lore.inference import key_scope
from lore.inference.key_scope import KeyRequired
from lore.inference.providers import factory
from lore.inference.providers.openai_compatible import MultiUserRefused
from tests.auth_fixtures import (
    BASE_URL, FAKE_KEY, Clock, FakeKeyChecker, give_key, make_app, make_settings,
    master_keys, sign_in,
)
from tests.central_fixtures import central_sessions

LOCAL = {"provider": "openai_compatible", "base_url": "http://host.docker.internal:8081/v1",
         "model": "qwen3-30b"}


def fake_server(requests: list):
    """An OpenAI-compatible server that answers {"ok": true}."""
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    return httpx.MockTransport(handle)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LORE_SELFHOST", "1")
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=("root", "alice"))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.commit()
    seen: list = []
    app = make_app(root, sessions, selfhost=True, auth=make_settings(selfhost=True),
                   providers={}, local_model_transport=fake_server(seen))
    return app, sessions, seen


def service(sessions):
    return KeyService(sessions, master_keys(), FakeKeyChecker(), clock=Clock(),
                      server_default=True)


# --- the admin sets it -----------------------------------------------------

def test_the_admin_sets_a_local_model_for_everyone(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.put("/api/admin/provider", json=LOCAL)
    assert r.status_code == 200, r.text
    saved = r.json()["provider"]
    assert (saved["provider"], saved["base_url"], saved["model"], saved["has_key"]) == (
        "openai_compatible", LOCAL["base_url"], "qwen3-30b", False)
    assert admin.get("/api/admin/provider").json()["provider"]["model"] == "qwen3-30b"


def test_a_user_without_a_key_then_works_on_the_admins_model(env):
    app, sessions, _ = env
    alice = sign_in(app, sessions, "alice")
    assert alice.get("/api/me/key").json()["present"] is False
    sign_in(app, sessions, "root").put("/api/admin/provider", json=LOCAL)
    status = alice.get("/api/me/key").json()
    assert status["present"] is True and status["server"] is True
    providers = alice.get("/api/me/providers").json()
    assert providers["server"] == {"provider": "openai_compatible", "model": "qwen3-30b"}
    assert "base_url" not in json.dumps(providers)  # the address stays the admin's business
    creds = service(sessions).credentials_for("alice")
    assert (creds.provider, creds.base_url, creds.source) == (
        "openai_compatible", LOCAL["base_url"], "server")


def test_the_bound_scope_builds_the_admins_local_model(env):
    _app, sessions, _ = env
    keys = service(sessions)
    keys.set_server_provider("root", **LOCAL)
    scope = keys.scope("alice")
    config = LoreConfig()
    with key_scope.bound(scope):
        creds = factory.credentials(config)
        assert (creds.provider, creds.base_url, creds.model) == (
            "openai_compatible", LOCAL["base_url"], "qwen3-30b")
        model = factory.build(creds, config, "write", gemini_models=())
    assert model.provider == "openai_compatible"


def test_a_users_own_key_wins_over_the_admins_provider(env):
    _app, sessions, _ = env
    keys = service(sessions)
    keys.set_server_provider("root", **LOCAL)
    give_key(sessions, "alice")
    creds = keys.credentials_for("alice")
    assert (creds.provider, creds.key, creds.source) == ("gemini", FAKE_KEY, "user")


def test_the_admin_sets_a_cloud_key_and_it_is_never_shown_back(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    r = admin.put("/api/admin/provider", json={"provider": "gemini", "key": FAKE_KEY})
    assert r.status_code == 200, r.text
    text = admin.get("/api/admin/provider").text
    assert FAKE_KEY not in text and FAKE_KEY[-4:] in text
    with sessions() as db:
        assert FAKE_KEY not in json.dumps(db.get(ServerSetting, "provider").value)
    assert service(sessions).credentials_for("alice").key == FAKE_KEY
    # Changing the model later keeps the key.
    assert admin.put("/api/admin/provider", json={"provider": "gemini"}).status_code == 200
    assert service(sessions).credentials_for("alice").key == FAKE_KEY


@pytest.mark.parametrize("body", [
    {"provider": "openai_compatible", "base_url": "file:///etc/passwd", "model": "m"},
    {"provider": "openai_compatible", "base_url": "http://x:1/v1", "model": ""},
    {"provider": "gemini"},  # no key yet
    {"provider": "nonsense", "key": FAKE_KEY}])
def test_a_bad_provider_setting_is_refused(env, body):
    app, sessions, _ = env
    r = sign_in(app, sessions, "root").put("/api/admin/provider", json=body)
    assert r.status_code == 400
    assert service(sessions).server_provider() is None


def test_the_admin_removes_it_and_users_need_a_key_again(env):
    app, sessions, _ = env
    admin = sign_in(app, sessions, "root")
    admin.put("/api/admin/provider", json=LOCAL)
    assert admin.delete("/api/admin/provider").json()["removed"] is True
    with pytest.raises(KeyRequired):
        service(sessions).credentials_for("alice")


def test_test_connection_calls_the_given_server(env):
    app, sessions, seen = env
    admin = sign_in(app, sessions, "root")
    r = admin.post("/api/admin/provider/test", json=LOCAL)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert str(seen[0].url).startswith(LOCAL["base_url"])


# --- the boundary ------------------------------------------------------------

def test_a_user_cannot_set_or_test_the_provider_for_everyone(env):
    app, sessions, seen = env
    alice = sign_in(app, sessions, "alice")
    assert alice.put("/api/admin/provider", json=LOCAL).status_code == 403
    assert alice.post("/api/admin/provider/test", json=LOCAL).status_code == 403
    assert alice.delete("/api/admin/provider").status_code == 403
    assert alice.get("/api/admin/provider").status_code == 403
    assert seen == [] and service(sessions).server_provider() is None


def test_a_user_cannot_give_themselves_a_base_url(env):
    app, sessions, _ = env
    alice = sign_in(app, sessions, "alice")
    r = alice.put("/api/me/key", json={"provider": "openai_compatible", "key": "sk-x" * 10,
                                       "base_url": "http://169.254.169.254/"})
    assert r.status_code == 400
    r = alice.put("/api/me/provider", json={"provider": "openai_compatible", "model": "m",
                                            "base_url": "http://169.254.169.254/"})
    assert r.status_code == 400
    # The local-model settings of local mode are not served here at all.
    assert alice.put("/api/local-model", json=LOCAL).status_code in (404, 405)
    assert alice.post("/api/local-model/test", json=LOCAL).status_code in (404, 405)
    with sessions() as db:
        assert db.scalars(sa.select(ServerSetting)).all() == []


def test_a_scope_with_a_base_url_but_not_from_the_server_is_refused_outside_self_host(
        monkeypatch):
    """Hosted multi mode: the provider is refused whatever the scope carries."""
    monkeypatch.delenv("LORE_SELFHOST", raising=False)
    monkeypatch.setenv("LORE_MULTI_USER", "1")
    scope = key_scope.KeyScope("alice", "no-key", provider="openai_compatible",
                               model="m", base_url="http://10.0.0.1/v1")
    with key_scope.bound(scope), pytest.raises(MultiUserRefused):
        factory.credentials(LoreConfig())


def test_self_host_refuses_a_local_scope_with_no_admin_address(monkeypatch):
    monkeypatch.setenv("LORE_SELFHOST", "1")
    scope = key_scope.KeyScope("alice", "no-key", provider="openai_compatible", model="m")
    with key_scope.bound(scope), pytest.raises(MultiUserRefused):
        factory.credentials(LoreConfig())


def test_the_hosted_site_has_no_provider_for_everyone(tmp_path, monkeypatch):
    monkeypatch.delenv("LORE_SELFHOST", raising=False)
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=("root", "alice"))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.commit()
    app = make_app(root, sessions)  # hosted multi mode, as before
    admin = sign_in(app, sessions, "root")
    assert admin.put("/api/admin/provider", json=LOCAL).status_code in (404, 405)
    assert admin.get("/api/admin/provider").status_code == 404
    assert TestClient(app, base_url=BASE_URL).post(
        "/auth/password", json={"email": "root@example.org", "password": "x" * 9}
    ).status_code in (404, 405)
    keys = KeyService(sessions, master_keys(), FakeKeyChecker(), clock=Clock())
    with pytest.raises(Exception):
        keys.set_server_provider("root", **LOCAL)
    with pytest.raises(KeyRequired):
        keys.credentials_for("alice")
