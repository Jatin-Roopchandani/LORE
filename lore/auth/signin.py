"""Sign-in routes: Google and GitHub (OAuth 2 + PKCE) and email magic links.

Every failure redirects to `/signin?error=<code>` with a fixed code and no
detail: the page cannot be used to learn why a provider or a token was
refused. Redirect targets are fixed paths, never taken from the request.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import sqlalchemy as sa
from authlib.common.security import generate_token
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from lore.auth import accounts, ratelimit
from lore.auth.accounts import Outcome
from lore.auth.cookies import (
    FLOW_COOKIE, SESSION_COOKIE, cleared_headers, cookie_header, session_headers,
)
from lore.auth.current import AuthContext
from lore.auth.mailer import MailError, sign_in_message
from lore.auth.providers import ProviderError
from lore.auth.sessions import LIFETIME, create_session, delete_session
from lore.auth.tokens import csrf_token, hash_token, new_token, same
from lore.central.models import LoginToken

log = logging.getLogger("lore.auth")

FLOW_MAX_AGE = 600           # an OAuth round trip, seconds
MAGIC_MAX_AGE = 15 * 60      # a magic link, seconds
EMAIL_PER_HOUR = 5
IP_PER_HOUR = 20
_FLOW_SALT = "lore.oauth-flow"
_MAGIC_SALT = "lore.magic-link"
_SIGNIN_ERRORS = {"oauth_failed", "oauth_denied", "email_unverified", "link_invalid",
                  "account_disabled"}


class EmailBody(BaseModel):
    email: str
    invite: str | None = None


def client_ip(request: Request, trust_proxy: bool) -> str:
    """The caller's address. Behind our own proxy (Caddy) that is the LAST
    X-Forwarded-For entry, the one the proxy appended; earlier ones are
    client-supplied and can be anything."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
    return request.client.host if request.client else "unknown"


def _redirect(path: str, extra: list[tuple[bytes, bytes]] | None = None
              ) -> RedirectResponse:
    response = RedirectResponse(path, status_code=303)
    # The URL can carry a code or token: never cache it, never send it on.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    for name, value in extra or []:
        response.headers.append(name.decode(), value.decode())
    return response


def build_signin_router(ctx: AuthContext) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings
    flow = URLSafeTimedSerializer(settings.session_secret, salt=_FLOW_SALT)
    magic = URLSafeTimedSerializer(settings.session_secret, salt=_MAGIC_SALT)

    def fail(code: str, *, clear_flow: bool = True) -> RedirectResponse:
        assert code in _SIGNIN_ERRORS
        extra = cleared_headers(settings, FLOW_COOKIE) if clear_flow else []
        return _redirect(f"/signin?error={code}", extra)

    def waitlist(clear_flow: bool = True) -> RedirectResponse:
        extra = cleared_headers(settings, FLOW_COOKIE) if clear_flow else []
        return _redirect("/waitlist", extra)

    def finish(request: Request, db, user_id: str) -> RedirectResponse:
        """A fresh session for the user (any old one for this browser goes, so
        a session id planted before sign-in is never promoted), then /app."""
        delete_session(db, request.cookies.get(SESSION_COOKIE))
        token = create_session(db, user_id, ctx.now(), request.headers.get("user-agent"))
        db.commit()
        csrf = csrf_token(settings.csrf_secret, token)
        return _redirect("/app", [
            *session_headers(settings, token, csrf, int(LIFETIME.total_seconds())),
            *cleared_headers(settings, FLOW_COOKIE)])

    def sign_in(db, provider: str, subject: str, email: str, name: str | None,
                invite_hash: str | None) -> accounts.SignIn:
        for attempt in (1, 2):
            try:
                return accounts.sign_in_with_identity(
                    db, provider, subject, email, name, invite_hash,
                    settings.invite_only, ctx.now())
            except IntegrityError:
                # Two sign-ins for one new email raced; the other won. Retry
                # once and this one finds the user it created.
                db.rollback()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    @router.get("/auth/providers")
    def providers():
        """What the sign-in page may offer (only providers that are configured)."""
        return {"providers": sorted(ctx.providers), "email": True,
                "invite_only": settings.invite_only}

    # --- OAuth ---------------------------------------------------------

    @router.get("/auth/{provider}/start")
    def start(provider: str, invite: str | None = None):
        prov = ctx.providers.get(provider)
        if prov is None:
            return JSONResponse({"error": {"code": "not_found",
                                           "message": "no such provider"}}, status_code=404)
        state = generate_token(32)
        verifier = generate_token(64)  # the PKCE secret; its hash goes to the provider
        carried = accounts.clean_invite(invite)
        cookie = flow.dumps({"p": provider, "s": state, "v": verifier, "i": carried})
        url = prov.authorize_url(redirect_uri=f"{settings.origin}/auth/{provider}/callback",
                                 state=state, code_verifier=verifier)
        return _redirect(url, [cookie_header(
            FLOW_COOKIE, cookie, max_age=FLOW_MAX_AGE, secure=settings.cookie_secure,
            http_only=True, path="/auth")])

    @router.get("/auth/{provider}/callback")
    def callback(provider: str, request: Request, code: str | None = None,
                 state: str | None = None, error: str | None = None):
        prov = ctx.providers.get(provider)
        if prov is None:
            return JSONResponse({"error": {"code": "not_found",
                                           "message": "no such provider"}}, status_code=404)
        # State and PKCE are mandatory: no cookie, a bad signature, an old
        # flow, another provider's flow, a missing or different `state`: all
        # end here, before the provider is asked anything.
        try:
            data = flow.loads(request.cookies.get(FLOW_COOKIE, ""), max_age=FLOW_MAX_AGE)
            expected, verifier = data["s"], data["v"]
            if data["p"] != provider or not isinstance(expected, str):
                raise BadSignature("flow is for another provider")
        except (BadSignature, KeyError, TypeError):
            return fail("oauth_failed")
        if not state or not same(state, expected):
            return fail("oauth_failed")
        if error or not code:
            return fail("oauth_denied")
        try:
            profile = prov.fetch_profile(
                code=code, code_verifier=verifier,
                redirect_uri=f"{settings.origin}/auth/{provider}/callback")
        except ProviderError as exc:
            log.warning("oauth: %s", exc)  # the type only; see providers.py
            return fail("oauth_failed")
        email = accounts.normalize_email(profile.email)
        # Only a verified email signs anyone in: an unverified one could be
        # somebody else's address, and email is how accounts are linked.
        if not (profile.email_verified and email and profile.subject):
            return fail("email_unverified")
        invite = accounts.clean_invite(data.get("i"))
        with ctx.sessions() as db:
            result = sign_in(db, provider, profile.subject, email, profile.name,
                             hash_token(invite) if invite else None)
            if result.outcome is Outcome.OK:
                return finish(request, db, result.user_id)
            db.rollback()
        if result.outcome is Outcome.NEEDS_INVITE:
            return waitlist()
        return fail("account_disabled")

    # --- magic links ---------------------------------------------------

    def send_link(email: str, link: str) -> None:
        subject, text, html = sign_in_message(link)
        try:
            ctx.mailer.send(to=email, subject=subject, text=text, html=html)
        except MailError as exc:
            log.warning("sign-in email not sent: %s", exc)

    @router.post("/auth/email", status_code=202)
    def email(body: EmailBody, request: Request, background: BackgroundTasks):
        """Ask for a sign-in link. The answer is 202 whether or not the address
        has an account, and the mail goes out after the response, so neither
        the body nor the timing tells a caller whether it exists."""
        address = accounts.normalize_email(body.email)
        if address is None:
            return JSONResponse({"error": {"code": "invalid_email",
                                           "message": "that does not look like an email address"}},
                                status_code=400)
        invite = accounts.clean_invite(body.invite)
        invite_hash = hash_token(invite) if invite else None
        now = ctx.now()
        with ctx.sessions() as db:
            ok = (ratelimit.allow(db, "ip", client_ip(request, settings.trust_proxy),
                                  IP_PER_HOUR, now)
                  and ratelimit.allow(db, "email", address, EMAIL_PER_HOUR, now))
            if not ok:
                db.commit()  # keep the hit that was counted
                return JSONResponse(
                    {"error": {"code": "rate_limited",
                               "message": "too many requests; try again later"}},
                    status_code=429, headers={"Retry-After": "3600"})
            if accounts.can_sign_in(db, address, invite_hash, now, settings.invite_only):
                raw = new_token()
                db.add(LoginToken(
                    token_hash=hash_token(raw), email=address, invite_hash=invite_hash,
                    created_at=now, expires_at=now + timedelta(seconds=MAGIC_MAX_AGE)))
                link = f"{settings.origin}/auth/email/verify?token={magic.dumps(raw)}"
                background.add_task(send_link, address, link)
            db.commit()
        return {"sent": True}

    @router.get("/auth/email/verify")
    def verify(request: Request, token: str = Query("", max_length=512)):
        try:
            raw = magic.loads(token, max_age=MAGIC_MAX_AGE)
        except BadSignature:  # tampered, or older than 15 minutes
            return fail("link_invalid", clear_flow=False)
        now = ctx.now()
        with ctx.sessions() as db:
            # Single use, atomically: of two requests with one link, one wins.
            claimed = db.execute(
                sa.update(LoginToken)
                .where(LoginToken.token_hash == hash_token(raw),
                       LoginToken.used_at.is_(None), LoginToken.expires_at > now)
                .values(used_at=now))
            if claimed.rowcount != 1:
                db.rollback()
                return fail("link_invalid", clear_flow=False)
            row = db.get(LoginToken, hash_token(raw))
            email_, invite_hash = row.email, row.invite_hash
            db.commit()  # the token is spent from here, whatever follows
            result = sign_in(db, "email", email_, email_, None, invite_hash)
            if result.outcome is Outcome.OK:
                return finish(request, db, result.user_id)
            db.rollback()
        if result.outcome is Outcome.NEEDS_INVITE:
            return waitlist(clear_flow=False)
        return fail("account_disabled", clear_flow=False)

    return router
