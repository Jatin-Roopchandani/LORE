"""Where to go after signing in, when it is not the app: an MCP client's consent page.

Opening /oauth/consent signed out remembers that page in a short-lived, signed,
HttpOnly cookie (`lore_next`) and sends the browser to /signin. Every way of
signing in ends in lore.auth.signin's `finish`, which asks `take` for the
remembered page and goes there instead of /app, clearing the cookie.

Only the consent page can be remembered: the value must be a signed path that
starts with CONSENT_PREFIX, so the cookie cannot become an open redirect.
"""
from __future__ import annotations

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from lore.auth.cookies import cookie_header
from lore.auth.settings import AuthSettings

NEXT_COOKIE = "lore_next"
MAX_AGE = 15 * 60  # long enough to sign in, magic link included
CONSENT_PREFIX = "/oauth/consent?req="
_SALT = "lore.next-page"
MAX_PATH_CHARS = 4096


def _signer(settings: AuthSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def allowed(path: object) -> bool:
    return (isinstance(path, str) and path.startswith(CONSENT_PREFIX)
            and len(path) <= MAX_PATH_CHARS
            and not any(c in path for c in ("\\", "\r", "\n", "#"))
            and "//" not in path)


def remember(settings: AuthSettings, path: str) -> tuple[bytes, bytes] | None:
    """The Set-Cookie header that remembers `path`, or None if it may not be."""
    if not allowed(path):
        return None
    return cookie_header(NEXT_COOKIE, _signer(settings).dumps(path), max_age=MAX_AGE,
                         secure=settings.cookie_secure, http_only=True)


def take(request: Request, settings: AuthSettings) -> str | None:
    """The remembered page, if the cookie is present, signed, fresh and allowed."""
    raw = request.cookies.get(NEXT_COOKIE)
    if not raw or len(raw) > 2 * MAX_PATH_CHARS:
        return None
    try:
        path = _signer(settings).loads(raw, max_age=MAX_AGE)
    except BadSignature:
        return None
    return path if allowed(path) else None


def cleared(settings: AuthSettings) -> tuple[bytes, bytes]:
    return cookie_header(NEXT_COOKIE, "", max_age=0, secure=settings.cookie_secure,
                         http_only=True)
