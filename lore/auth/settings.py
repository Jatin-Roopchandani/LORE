"""Auth configuration, read once from the environment (never from a request).

Secrets are required in multi mode and the app refuses to start without them:
a server that signs cookies with a default key is worse than one that is down.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

ENV_SESSION_SECRET = "LORE_SESSION_SECRET"
ENV_CSRF_SECRET = "LORE_CSRF_SECRET"
ENV_BASE_URL = "LORE_BASE_URL"
ENV_COOKIE_SECURE = "LORE_COOKIE_SECURE"
ENV_INVITE_ONLY = "LORE_INVITE_ONLY"
ENV_TRUST_PROXY = "LORE_TRUST_PROXY"
ENV_RESEND_KEY = "LORE_RESEND_API_KEY"
ENV_MAIL_FROM = "LORE_MAIL_FROM"
ENV_MAIL_REPLY_TO = "LORE_MAIL_REPLY_TO"  # optional: where replies to LORE mail go
ENV_NOTIFY_EMAIL = "LORE_NOTIFY_EMAIL"  # optional: who hears of each new invite request
MIN_SECRET_CHARS = 32


class AuthConfigError(RuntimeError):
    """The multi-user auth configuration is missing or unsafe."""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str = field(repr=False)  # never in a log line or a repr


@dataclass(frozen=True)
class AuthSettings:
    # Signs the OAuth flow cookie and the magic-link token.
    session_secret: str = field(repr=False)
    # Keys the per-session CSRF token (HMAC of the session cookie).
    csrf_secret: str = field(repr=False)
    # The public origin, e.g. https://lore.jrvn.site. Redirect URIs and mailed
    # links are built from this, never from the request's Host header.
    base_url: str = "http://127.0.0.1:8766"
    cookie_secure: bool = True
    invite_only: bool = True
    # Behind Caddy: take the client address from X-Forwarded-For (last entry).
    trust_proxy: bool = False
    google: OAuthClient | None = None
    github: OAuthClient | None = None
    resend_api_key: str | None = field(default=None, repr=False)
    mail_from: str = "LORE <login@lore.jrvn.site>"
    mail_reply_to: str | None = None
    # Mailed a short note for each new waitlist request; None: no note.
    notify_email: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("session", self.session_secret),
                            ("CSRF", self.csrf_secret)):
            if len(value) < MIN_SECRET_CHARS:
                raise AuthConfigError(
                    f"the {name} secret must be at least {MIN_SECRET_CHARS} characters")
        if self.session_secret == self.csrf_secret:
            raise AuthConfigError("the session and CSRF secrets must differ")
        if self.cookie_secure and not self.base_url.startswith("https://"):
            raise AuthConfigError(
                "LORE_BASE_URL must be https:// while secure cookies are on"
                f" (set {ENV_COOKIE_SECURE}=0 only for local http development)")

    @property
    def origin(self) -> str:
        return self.base_url.rstrip("/")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AuthSettings":
        env = os.environ if environ is None else environ

        def flag(name: str, default: bool) -> bool:
            raw = env.get(name, "").strip().lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on")

        def client(prefix: str) -> OAuthClient | None:
            cid = env.get(f"LORE_{prefix}_CLIENT_ID", "").strip()
            secret = env.get(f"LORE_{prefix}_CLIENT_SECRET", "").strip()
            return OAuthClient(cid, secret) if cid and secret else None

        missing = [n for n in (ENV_SESSION_SECRET, ENV_CSRF_SECRET, ENV_BASE_URL)
                   if not env.get(n, "").strip()]
        if missing:
            raise AuthConfigError("multi-user mode needs " + ", ".join(missing))
        return cls(
            session_secret=env[ENV_SESSION_SECRET].strip(),
            csrf_secret=env[ENV_CSRF_SECRET].strip(),
            base_url=env[ENV_BASE_URL].strip(),
            cookie_secure=flag(ENV_COOKIE_SECURE, True),
            invite_only=flag(ENV_INVITE_ONLY, True),
            trust_proxy=flag(ENV_TRUST_PROXY, False),
            google=client("GOOGLE"), github=client("GITHUB"),
            resend_api_key=env.get(ENV_RESEND_KEY, "").strip() or None,
            mail_from=env.get(ENV_MAIL_FROM, "").strip() or cls.mail_from,
            mail_reply_to=env.get(ENV_MAIL_REPLY_TO, "").strip() or None,
            notify_email=env.get(ENV_NOTIFY_EMAIL, "").strip() or None,
        )
