"""Shared pieces for the auth tests: settings, a signed-in client, no network."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from lore.auth import sessions as auth_sessions
from lore.auth.cookies import CSRF_COOKIE, SESSION_COOKIE
from lore.auth.settings import AuthSettings
from lore.auth.tokens import csrf_token
from lore.config import LoreConfig
from lore.web.app import create_app

SESSION_SECRET = "s" * 40
CSRF_SECRET = "c" * 40
BASE_URL = "http://127.0.0.1:8766"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def make_settings(**overrides) -> AuthSettings:
    # Plain-http test client: the cookie jar will not send Secure cookies, so
    # the default here is off; tests of the flags turn it on.
    fields = dict(session_secret=SESSION_SECRET, csrf_secret=CSRF_SECRET,
                  base_url=BASE_URL, cookie_secure=False)
    fields.update(overrides)
    return AuthSettings(**fields)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# Release step 3: every multi-mode app needs a master key for the users'
# Gemini keys, and checks a pasted key through a KeyChecker (faked: no network).
TEST_MASTER = bytes(range(32))
FAKE_KEY = "AIzaFAKEfakeFAKEfakeFAKEfakeFAKEfake123"  # shaped like a key, not one


def master_keys():
    from lore.central.keys import MasterKeys
    return MasterKeys(TEST_MASTER)


class FakeKeyChecker:
    """Answers a key check without the network; `result` is what Gemini says."""

    def __init__(self, result: str = "ok") -> None:
        self.result, self.seen = result, []

    def check(self, key: str) -> str:
        self.seen.append(key)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def give_key(sessions, user_id: str, key: str = FAKE_KEY) -> None:
    """Store `key` for `user_id` as a saved, checked key (no request)."""
    from lore.central.keys import KeyService
    KeyService(sessions, master_keys(), FakeKeyChecker(), clock=Clock()).save(user_id, key)


def make_app(root: LoreConfig, sessions, *, auth_clock: Clock | None = None,
             **kwargs):
    """A multi-mode app on a migrated central DB; `clock` (processing) and
    `auth_clock` (sessions) are separate, as in create_app."""
    kwargs.setdefault("auth", make_settings())
    kwargs.setdefault("master_keys", master_keys())
    kwargs.setdefault("key_checker", FakeKeyChecker())
    return create_app(root, multi_user=True, central_sessions=sessions,
                      auth_clock=auth_clock or Clock(), **kwargs)


def sign_in(app, sessions, user_id: str, *, now: datetime = NOW,
            settings: AuthSettings | None = None) -> TestClient:
    """A client holding a real session for `user_id` and sending its CSRF
    token on every request (as the web client does)."""
    settings = settings or make_settings()
    with sessions() as db:
        token = auth_sessions.create_session(db, user_id, now)
        db.commit()
    client = TestClient(app, base_url=BASE_URL)
    client.cookies.set(SESSION_COOKIE, token)
    csrf = csrf_token(settings.csrf_secret, token)
    client.cookies.set(CSRF_COOKIE, csrf)
    client.headers["X-CSRF-Token"] = csrf
    client.session_token = token
    return client


def open_magic_link(client, link: str):
    """What a browser does with a mailed sign-in link (release step 6): the
    GET only draws the "Continue signing in" page and spends nothing; its
    button then POSTs the token. Returns the POST's response."""
    from urllib.parse import parse_qs, urlparse
    path = link[len(BASE_URL):] if link.startswith(BASE_URL) else link
    page = client.get(path)
    assert page.status_code == 200, page.text
    token = parse_qs(urlparse(path).query).get("token", [""])[0]
    return client.post("/auth/email/verify", data={"token": token})


# --- a bridge for tests written as "act as this user" -----------------------

ACT_AS = "X-Test-User"  # a header only these tests' client understands


class ActAsClient(TestClient):
    """A TestClient that turns `headers={ACT_AS: "alice"}` into alice's REAL
    session cookie and CSRF token, so each request still goes through the
    cookie -> session row -> user hook. The header never reaches the app. A
    user with no row in the central `users` table gets no session (a plain
    anonymous request), which is exactly what the app would see."""

    def __init__(self, app, sessions, **kwargs) -> None:
        super().__init__(app, base_url=BASE_URL, **kwargs)
        self._app, self._sessions, self._by_user = app, sessions, {}

    def _client_for(self, user: str) -> TestClient:
        if user not in self._by_user:
            from lore.central.models import User
            with self._sessions() as db:
                known = db.get(User, user) is not None
            self._by_user[user] = (sign_in(self._app, self._sessions, user) if known
                                   else TestClient(self._app, base_url=BASE_URL))
        return self._by_user[user]

    def _route(self, kwargs):
        headers = dict(kwargs.get("headers") or {})
        user = headers.pop(ACT_AS, None)
        kwargs["headers"] = headers
        return (None if user is None else self._client_for(user)), kwargs

    def request(self, method, url, **kwargs):
        target, kwargs = self._route(kwargs)
        if target is None:
            return super().request(method, url, **kwargs)
        return target.request(method, url, **kwargs)

    def stream(self, method, url, **kwargs):
        target, kwargs = self._route(kwargs)
        if target is None:
            return super().stream(method, url, **kwargs)
        return target.stream(method, url, **kwargs)


# --- doubles for the outside world ------------------------------------------

class FakeMailer:
    """Collects mail instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    def send(self, *, to, subject, text, html=None) -> None:
        if self.fail:
            from lore.auth.mailer import MailError
            raise MailError("boom")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})

    def links(self) -> list[str]:
        import re
        return [m for mail in self.sent
                for m in re.findall(r"https?://\S+", mail["text"])]


class FakeProvider:
    """A provider that returns whatever profile the test sets, and records the
    authorize and token requests it was asked to make."""

    def __init__(self, name: str = "google") -> None:
        from lore.auth.providers import Profile
        self.name = name
        self.profile = Profile("sub-1", "reader@example.org", True, "Reader")
        self.error = False
        self.authorized: list[dict] = []
        self.exchanged: list[dict] = []

    def authorize_url(self, *, redirect_uri, state, code_verifier):
        self.authorized.append({"redirect_uri": redirect_uri, "state": state,
                                "code_verifier": code_verifier})
        return f"https://provider.example/authorize?state={state}"

    def fetch_profile(self, *, code, code_verifier, redirect_uri):
        from lore.auth.providers import ProviderError
        self.exchanged.append({"code": code, "code_verifier": code_verifier,
                               "redirect_uri": redirect_uri})
        if self.error:
            raise ProviderError("provider refused")
        return self.profile
