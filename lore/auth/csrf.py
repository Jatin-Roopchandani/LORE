"""CSRF: every state-changing request must carry the session's token.

The token is HMAC(csrf secret, session cookie), delivered in a readable cookie
(`lore_csrf`) and echoed by the page's script in `X-CSRF-Token`. A cross-site
page can make the browser send the cookies but can neither read the token nor
set the header, and an attacker cannot compute it without the secret.

Applied to every route of the app as a dependency, by METHOD, not by a list of
paths: a mutating route added later is protected without anyone remembering.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from lore.auth.cookies import SESSION_COOKIE
from lore.auth.tokens import csrf_token, same

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Public endpoints with no session to forge: a visitor has no ambient
# authority for a cross-site page to borrow. Each has its own abuse limits
# (rate limit, honeypot) and the Origin check in front of it.
# POST /auth/email/verify: the magic-link token in the body is itself the
# proof, and a cross-site form is refused by the Origin guard (login CSRF).
# Self-host mode adds three of the same kind (lore.auth.selfhost): the
# first-run setup (open only while there is no admin at all), password
# sign-in and the set-password link (the password, or the link's code, is
# the proof).
EXEMPT_PATHS = frozenset({"/api/waitlist", "/auth/email", "/auth/email/verify",
                          "/api/setup", "/auth/password", "/auth/password/link"})
HEADER = "x-csrf-token"


def csrf_guard(request: Request) -> None:
    if request.method in SAFE_METHODS or request.url.path in EXEMPT_PATHS:
        return
    ctx = getattr(request.app.state, "auth", None)
    if ctx is None:  # local mode: no sessions, nothing to forge
        return
    session = request.cookies.get(SESSION_COOKIE)
    if not session:
        # No session cookie: there is no signed-in user to impersonate, and
        # the resolver answers 401. (The only resolver is the cookie one.)
        return
    sent = request.headers.get(HEADER)
    if not sent or not same(sent, csrf_token(ctx.settings.csrf_secret, session)):
        raise HTTPException(403, "missing or invalid CSRF token")
