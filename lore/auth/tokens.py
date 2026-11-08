"""Bearer secrets: make them, hash them, compare them.

A token is 256 random bits. Only its SHA-256 is stored, so a copy of the
database holds nothing that can be presented as a cookie, a link or a code.
SHA-256 (not a slow hash) is right here: the input is already 256 bits of
entropy, so there is nothing to brute-force.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    """Constant-time equality of two strings."""
    return hmac.compare_digest(a.encode(), b.encode())


def csrf_token(secret: str, session_token: str) -> str:
    """The per-session CSRF token: HMAC(secret, session cookie).

    Bound to the session, so one user's token is useless with another's cookie,
    and nothing needs storing. The browser holds it in a readable cookie and
    echoes it in a header, which a cross-site page can do neither of.
    """
    return hmac.new(secret.encode(), b"csrf:" + session_token.encode(),
                    hashlib.sha256).hexdigest()
