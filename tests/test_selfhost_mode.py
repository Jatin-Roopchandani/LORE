"""Self-host mode's plumbing: secrets made and kept, settings defaults, the
landing page, the Host check, and cookies on plain http."""
import os
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from lore.auth.passwords import hash_password
from lore.auth.settings import AuthConfigError, AuthSettings
from lore.central.keys import MasterKeys
from lore.central.models import User
from lore.config import LoreConfig
from lore.selfhost import SECRETS_FILE, apply_secrets, ensure_secrets
from tests.auth_fixtures import BASE_URL, make_app, make_settings, sign_in
from tests.central_fixtures import central_sessions

NAMES = ("LORE_SESSION_SECRET", "LORE_CSRF_SECRET", "LORE_KEY_ENCRYPTION_KEY")


# --- secrets -----------------------------------------------------------------

def test_secrets_are_made_once_and_kept_in_the_data_root(tmp_path):
    first = ensure_secrets(tmp_path, environ={})
    path = tmp_path / SECRETS_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ensure_secrets(tmp_path, environ={}) == first
    assert len(set(first.values())) == 3
    # Usable as they are: AuthSettings and MasterKeys accept them.
    AuthSettings.from_env({**first, "LORE_SELFHOST": "1"})
    MasterKeys.from_env(first)


def test_the_environment_overrides_a_stored_secret(tmp_path):
    stored = ensure_secrets(tmp_path, environ={})
    mine = "m" * 48
    got = ensure_secrets(tmp_path, environ={"LORE_SESSION_SECRET": mine})
    assert got["LORE_SESSION_SECRET"] == mine
    assert got["LORE_CSRF_SECRET"] == stored["LORE_CSRF_SECRET"]


def test_with_every_secret_given_no_file_is_made(tmp_path):
    given = {n: v for n, v in zip(NAMES, ("a" * 40, "b" * 40, "x"))}
    assert ensure_secrets(tmp_path, environ=given) == given
    assert not (tmp_path / SECRETS_FILE).exists()


def test_processes_starting_together_agree_on_one_set(tmp_path):
    barrier = threading.Barrier(6)
    seen = []

    def start():
        barrier.wait()
        seen.append(ensure_secrets(tmp_path, environ={}))

    threads = [threading.Thread(target=start) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s == seen[0] for s in seen)
    assert [p.name for p in tmp_path.iterdir()] == [SECRETS_FILE]  # no temp files left


def test_apply_secrets_fills_the_environment_only_in_self_host_mode(tmp_path):
    env = {}
    apply_secrets(tmp_path, env)
    assert env == {} and not (tmp_path / SECRETS_FILE).exists()
    env = {"LORE_SELFHOST": "1", "LORE_CSRF_SECRET": "c" * 40}
    apply_secrets(tmp_path, env)
    assert env["LORE_CSRF_SECRET"] == "c" * 40
    assert all(env.get(n) for n in NAMES)


def test_a_self_hosted_app_starts_with_no_secrets_set(tmp_path, monkeypatch):
    """`docker compose up -d` with no .env: the app makes and keeps its secrets."""
    for name in ("LORE_BASE_URL", "LORE_COOKIE_SECURE", "LORE_MULTI_USER"):
        monkeypatch.delenv(name, raising=False)
    for name in NAMES:  # empty now, put back as they were (unset) afterwards
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("LORE_SELFHOST", "1")
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=())
    from lore.web.app import create_app
    app = create_app(root, central_sessions=sessions)
    assert (tmp_path / "root" / SECRETS_FILE).exists()
    assert TestClient(app, base_url=BASE_URL).get("/api/setup").json() == {"needed": True}


# --- settings ----------------------------------------------------------------

SECRETS = {"LORE_SESSION_SECRET": "s" * 40, "LORE_CSRF_SECRET": "c" * 40}


def test_self_host_defaults_to_localhost_and_plain_http_cookies():
    s = AuthSettings.from_env({**SECRETS, "LORE_SELFHOST": "1"})
    assert s.selfhost and s.base_url == "http://localhost:8080" and s.cookie_secure is False


def test_an_https_address_turns_secure_cookies_on():
    s = AuthSettings.from_env({**SECRETS, "LORE_SELFHOST": "1",
                               "LORE_BASE_URL": "https://lore.example.org"})
    assert s.cookie_secure is True


def test_the_cookie_switch_still_overrides():
    s = AuthSettings.from_env({**SECRETS, "LORE_SELFHOST": "1",
                               "LORE_BASE_URL": "https://lore.example.org",
                               "LORE_COOKIE_SECURE": "0"})
    assert s.cookie_secure is False
    with pytest.raises(AuthConfigError):  # Secure cookies on plain http: refused
        AuthSettings.from_env({**SECRETS, "LORE_SELFHOST": "1", "LORE_COOKIE_SECURE": "1"})


def test_the_hosted_settings_are_unchanged():
    with pytest.raises(AuthConfigError):
        AuthSettings.from_env(SECRETS)  # LORE_BASE_URL is still required
    s = AuthSettings.from_env({**SECRETS, "LORE_BASE_URL": "https://lore.jrvn.site"})
    assert s.cookie_secure is True and not s.selfhost


# --- the running app ---------------------------------------------------------

@pytest.fixture
def app_env(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=("root", "alice"))
    with sessions() as db:
        db.get(User, "root").role = "admin"
        db.get(User, "alice").password_hash = hash_password("alice password")
        db.commit()
    app = make_app(root, sessions, selfhost=True, auth=make_settings(selfhost=True),
                   providers={})
    return app, sessions


def anon(app, base_url=BASE_URL):
    return TestClient(app, base_url=base_url, follow_redirects=False)


def test_signed_out_visitors_land_on_sign_in_not_a_homepage(app_env):
    app, sessions = app_env
    assert anon(app).get("/").headers["location"] == "/signin"
    assert sign_in(app, sessions, "alice").get("/", follow_redirects=False).headers[
        "location"] == "/app"


@pytest.mark.parametrize("path", ["/blog", "/faq", "/blog/some-post", "/privacy", "/terms",
                                  "/waitlist", "/sitemap.xml", "/llms.txt"])
def test_there_are_no_public_marketing_pages(app_env, path):
    app, _ = app_env
    r = anon(app).get(path)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_robots_are_kept_out(app_env):
    app, _ = app_env
    assert "Disallow: /" in anon(app).get("/robots.txt").text


@pytest.mark.parametrize("host", ["localhost:9000", "127.0.0.1:8080", "192.168.1.20:8080",
                                  "[::1]:8080", "10.0.0.5"])
def test_any_local_or_lan_address_is_answered(app_env, host):
    app, _ = app_env
    assert anon(app).get("/healthz", headers={"Host": host}).status_code == 200


def test_another_host_name_is_refused_unless_allowed(tmp_path, monkeypatch, app_env):
    app, _ = app_env
    assert anon(app).get("/healthz", headers={"Host": "evil.example:8080"}).status_code == 403
    monkeypatch.setenv("LORE_ALLOWED_HOSTS", "lore.home.arpa, nas.local:8080")
    root = LoreConfig(data_dir=tmp_path / "other")
    (tmp_path / "x").mkdir()
    sessions, _url = central_sessions(tmp_path / "x", users=())
    allowed = make_app(root, sessions, selfhost=True, auth=make_settings(selfhost=True),
                       providers={})
    for host in ("lore.home.arpa", "lore.home.arpa:8080", "nas.local:8080"):
        assert anon(allowed).get("/healthz", headers={"Host": host}).status_code == 200
    assert anon(allowed).get("/healthz", headers={"Host": "nas.local:9999"}).status_code == 403


def test_the_base_url_host_is_answered(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=())
    app = make_app(root, sessions, selfhost=True, providers={},
                   auth=make_settings(selfhost=True, base_url="https://lore.example.org",
                                      cookie_secure=True))
    r = anon(app, "https://lore.example.org").get("/healthz")
    assert r.status_code == 200


def test_a_page_on_another_origin_is_refused(app_env):
    app, _ = app_env
    r = anon(app, "http://192.168.1.20:8080").post(
        "/auth/password", json={"email": "alice@example.org", "password": "alice password"},
        headers={"Origin": "http://192.168.1.21:8080"})
    assert r.status_code == 403
    ok = anon(app, "http://192.168.1.20:8080").post(
        "/auth/password", json={"email": "alice@example.org", "password": "alice password"},
        headers={"Origin": "http://192.168.1.20:8080"})
    assert ok.status_code == 200


def test_cookies_on_plain_http_are_not_secure_and_sign_in_works_on_a_lan_address(app_env):
    app, _ = app_env
    client = anon(app, "http://192.168.1.20:8080")
    r = client.post("/auth/password", json={"email": "alice@example.org",
                                            "password": "alice password"})
    cookies = r.headers.get_list("set-cookie")
    assert cookies and not any("Secure" in c for c in cookies)
    assert all("SameSite=Lax" in c for c in cookies)
    assert client.get("/api/me").status_code == 200


def test_https_base_url_gives_secure_cookies(tmp_path):
    root = LoreConfig(data_dir=tmp_path / "root")
    sessions, _url = central_sessions(tmp_path, users=("alice",))
    with sessions() as db:
        db.get(User, "alice").password_hash = hash_password("alice password")
        db.add(User(id="root", email="root@example.org", role="admin"))
        db.commit()
    app = make_app(root, sessions, selfhost=True, providers={},
                   auth=make_settings(selfhost=True, base_url="https://lore.example.org",
                                      cookie_secure=True))
    r = anon(app, "https://lore.example.org").post(
        "/auth/password", json={"email": "alice@example.org", "password": "alice password"})
    assert r.status_code == 200
    assert all("Secure" in c for c in r.headers.get_list("set-cookie"))


def test_the_page_is_marked_self_hosted(app_env):
    from lore.web.app import STATIC
    if not (STATIC / "index.html").exists():
        pytest.skip("the web app is not built")
    app, _ = app_env
    page = anon(app).get("/signin")
    assert 'name="lore-selfhost"' in page.text and 'name="lore-multi"' in page.text


def test_self_host_turns_multi_user_on(monkeypatch):
    from lore import tenancy
    from lore.inference import key_scope
    monkeypatch.delenv("LORE_MULTI_USER", raising=False)
    monkeypatch.setenv("LORE_SELFHOST", "1")
    assert tenancy.multi_user_enabled() and key_scope.multi_user_enabled()
    monkeypatch.delenv("LORE_SELFHOST")
    assert not tenancy.multi_user_enabled()


def test_secrets_never_show_in_the_settings_repr(tmp_path):
    secrets = ensure_secrets(tmp_path, environ={})
    s = AuthSettings.from_env({**secrets, "LORE_SELFHOST": "1"})
    assert not any(v in repr(s) for v in secrets.values())
    assert os.environ.get("LORE_SESSION_SECRET") != secrets["LORE_SESSION_SECRET"]
