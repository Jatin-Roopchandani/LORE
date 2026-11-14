"""Self-host mode's routes: first-run setup, password sign-in, people, the provider.

Mounted only in self-host mode (LORE_SELFHOST; lore.selfhost). Every POST,
PUT and DELETE is behind the app-wide CSRF guard, except the three that run
before there is a session (setup, sign-in, the set-password link): those have
no ambient authority to borrow, and the Origin guard refuses a cross-site form.

- GET  /api/setup                 {needed}
- POST /api/setup                 {code, email, password, confirm, name?}: the
                                  first admin, once, with the setup code from
                                  the server's log (lore.auth.firstrun)
- POST /auth/password             {email, password}: sign in
- GET  /auth/password/link?code=  the address a set-password link is for
- POST /auth/password/link        {code, password}: set it, and sign in
- POST /api/me/password           {current, password}: change my own
- POST /api/admin/users           {email, name?, password?}: add a person; with
                                  no password, a set-password link to copy
- POST /api/admin/users/{id}/password {password?}: reset theirs (or a link)
- GET/PUT/DELETE /api/admin/provider, POST /api/admin/provider/test: the AI
  provider for everyone (lore.central.keys), the admin's alone. The local
  model's address is accepted here and nowhere else.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from lore.auth import accounts, ratelimit
from lore.auth import next as next_page
from lore.auth.admin import log_event, make_require_admin
from lore.auth.cookies import SESSION_COOKIE, session_headers
from lore.auth.current import AuthContext, session_user
from lore.auth.firstrun import (
    SetupDone, create_first_admin, forget_setup_code, setup_code_matches, setup_needed,
)
from lore.auth.passwords import PasswordPolicyError, check_policy, hash_password, verify_password
from lore.auth.sessions import LIFETIME, create_session, delete_session, delete_user_sessions
from lore.auth.signin import client_ip
from lore.auth.tokens import csrf_token, hash_token
from lore.central.models import AuthSession, Invite, User

log = logging.getLogger("lore.auth")

# Sign-in attempts: per address in a quarter of an hour, per client in an hour.
EMAIL_ATTEMPTS = 10
EMAIL_WINDOW = timedelta(minutes=15)
IP_ATTEMPTS = 60
SETUP_ATTEMPTS = 10          # per client a quarter hour, while setup is open
SETUP_WINDOW = timedelta(minutes=15)
SETUP_CODE_HELP = ("Enter the setup code from the server's log. With Docker:"
                   ' docker compose logs lore | grep "setup code"')
LINK_LIFETIME = timedelta(days=7)
PROVIDER_TESTS_PER_HOUR = 30

INVALID = {"code": "invalid_credentials", "message": "That email and password don't match."}


def _error(status: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store", **headers})


async def _body(request: Request) -> dict:
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        body = None
    return body if isinstance(body, dict) else {}


def _text(value: object, limit: int = 320) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def build_selfhost_router(ctx: AuthContext, key_service, *, transport=None) -> APIRouter:
    """`key_service`: the app's KeyService (made with server_default=True).
    `transport`: the HTTP transport the local-model test uses (tests fake it)."""
    router = APIRouter()
    settings = ctx.settings
    require_admin = make_require_admin(ctx)

    def signed_in(request: Request, db, user_id: str, status: int = 200) -> JSONResponse:
        """A fresh session for the user (any old one for this browser goes),
        and where the page should go next."""
        delete_session(db, request.cookies.get(SESSION_COOKIE))
        token = create_session(db, user_id, ctx.now(), request.headers.get("user-agent"))
        db.commit()
        target = next_page.take(request, settings) or "/app"
        response = JSONResponse({"ok": True, "redirect": target}, status_code=status,
                                headers={"Cache-Control": "no-store"})
        extra = [next_page.cleared(settings)] if request.cookies.get(next_page.NEXT_COOKIE) else []
        for name, value in [*session_headers(settings, token,
                                              csrf_token(settings.csrf_secret, token),
                                              int(LIFETIME.total_seconds())), *extra]:
            response.headers.append(name.decode(), value.decode())
        return response

    def new_password(raw: object) -> str | JSONResponse:
        try:
            return hash_password(check_policy(raw))
        except PasswordPolicyError as exc:
            return _error(400, "weak_password", str(exc))

    def set_password_link(db, admin: str, email: str) -> dict:
        code = accounts.create_invite(db, admin, ctx.now(), LINK_LIFETIME, email=email)
        return {"code": code, "link": f"{settings.origin}/set-password?code={code}",
                "expires_at": (ctx.now() + LINK_LIFETIME).isoformat()}

    # --- first run ---------------------------------------------------------

    @router.get("/api/setup")
    def setup_state():
        with ctx.sessions() as db:
            return JSONResponse({"needed": setup_needed(db)},
                                headers={"Cache-Control": "no-store"})

    @router.post("/api/setup", status_code=201)
    async def setup(request: Request):
        body = await _body(request)
        now = ctx.now()
        with ctx.sessions() as db:
            if not setup_needed(db):
                return _error(409, "setup_done", "The admin account already exists. Sign in.")
            allowed = ratelimit.allow(db, "setup", client_ip(request, settings.trust_proxy),
                                      SETUP_ATTEMPTS, now, window=SETUP_WINDOW)
            db.commit()
        if not allowed:
            return _error(429, "rate_limited", "Too many tries. Wait a while and try again.",
                          **{"Retry-After": "900"})
        if not setup_code_matches(ctx.data_root, body.get("code")):
            return _error(403, "setup_code", SETUP_CODE_HELP)
        email = accounts.normalize_email(body.get("email"))
        if email is None:
            return _error(400, "invalid_email", "That doesn't look like an email address.")
        password = body.get("password")
        if password != body.get("confirm"):
            return _error(400, "password_mismatch", "The two passwords don't match.")
        hashed = new_password(password)
        if isinstance(hashed, JSONResponse):
            return hashed
        try:
            admin = create_first_admin(ctx.sessions, email, hashed, now,
                                       name=_text(body.get("name"), 200))
        except SetupDone:
            return _error(409, "setup_done", "The admin account already exists. Sign in.")
        forget_setup_code(ctx.data_root)  # never valid, or printed, again
        log.info("first-run setup: the admin account was created")
        with ctx.sessions() as db:
            log_event(db, admin, "setup", admin, now=now)
            return signed_in(request, db, admin, status=201)

    # --- signing in ----------------------------------------------------------

    @router.post("/auth/password")
    async def password_sign_in(request: Request):
        body = await _body(request)
        email = accounts.normalize_email(body.get("email"))
        password = body.get("password")
        now = ctx.now()
        with ctx.sessions() as db:
            ok = ratelimit.allow(db, "pw_ip", client_ip(request, settings.trust_proxy),
                                 IP_ATTEMPTS, now)
            if ok and email is not None:
                ok = ratelimit.allow(db, "pw_email", email, EMAIL_ATTEMPTS, now,
                                     window=EMAIL_WINDOW)
            db.commit()  # every attempt counts, the refused one too
            if not ok:
                return _error(429, "rate_limited",
                              "Too many tries. Wait a few minutes and try again.",
                              **{"Retry-After": "900"})
            user = accounts.user_by_email(db, email) if email else None
            # The same work and the same answer whether or not the account exists.
            good = verify_password(user.password_hash if user else None, password)
            if not good or user is None or user.status != "active":
                return _error(401, INVALID["code"], INVALID["message"])
            return signed_in(request, db, user.id)

    def link_target(db, code: object):
        """(invite row, user) for a usable set-password link, else None."""
        clean = accounts.clean_invite(code)
        if clean is None:
            return None
        row = db.get(Invite, hash_token(clean))
        now = ctx.now()
        if (row is None or row.used_by is not None or row.email is None
                or (row.expires_at is not None and row.expires_at <= now)):
            return None
        user = accounts.user_by_email(db, row.email)
        if user is None or user.status != "active":
            return None
        return row, user

    @router.get("/auth/password/link")
    def password_link(code: str = ""):
        with ctx.sessions() as db:
            found = link_target(db, code)
            if found is None:
                return _error(404, "link_invalid", "This link has expired or was already used."
                                                   " Ask your admin for a new one.")
            return JSONResponse({"email": found[1].email},
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @router.post("/auth/password/link")
    async def use_password_link(request: Request):
        body = await _body(request)
        now = ctx.now()
        with ctx.sessions() as db:
            allowed = ratelimit.allow(db, "pw_link", client_ip(request, settings.trust_proxy),
                                      IP_ATTEMPTS, now)
            db.commit()
        if not allowed:
            return _error(429, "rate_limited", "Too many tries. Wait a while and try again.")
        hashed = new_password(body.get("password"))
        if isinstance(hashed, JSONResponse):
            return hashed
        with ctx.sessions() as db:
            found = link_target(db, body.get("code"))
            if found is None:
                return _error(400, "link_invalid", "This link has expired or was already used."
                                                   " Ask your admin for a new one.")
            row, user = found
            # Single use, atomically: of two requests with one link, one wins.
            claimed = db.execute(sa.update(Invite).where(
                Invite.code_hash == row.code_hash, Invite.used_by.is_(None)
            ).values(used_by=user.id))
            if claimed.rowcount != 1:
                db.rollback()
                return _error(400, "link_invalid", "This link was already used.")
            user.password_hash = hashed
            delete_user_sessions(db, user.id)
            db.commit()
            return signed_in(request, db, user.id)

    @router.post("/api/me/password")
    async def change_password(request: Request):
        """My own password: the current one, then the new one. My other
        devices are signed out; this one stays signed in."""
        user = session_user(request)
        if user is None:
            return _error(401, "unauthorized", "sign in required")
        body = await _body(request)
        now = ctx.now()
        with ctx.sessions() as db:
            allowed = ratelimit.allow(db, "pw_change", user.id, EMAIL_ATTEMPTS, now,
                                      window=EMAIL_WINDOW)
            db.commit()
            if not allowed:
                return _error(429, "rate_limited", "Too many tries. Wait a few minutes.")
            row = db.get(User, user.id)
            if row.password_hash is not None and not verify_password(
                    row.password_hash, body.get("current")):
                return _error(400, "wrong_password", "Your current password isn't right.")
        hashed = new_password(body.get("password"))
        if isinstance(hashed, JSONResponse):
            return hashed
        keep = hash_token(request.cookies.get(SESSION_COOKIE, ""))
        with ctx.sessions() as db:
            db.get(User, user.id).password_hash = hashed
            db.execute(sa.delete(AuthSession).where(AuthSession.user_id == user.id,
                                                    AuthSession.token_hash != keep))
            db.commit()
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    # --- the admin: people -----------------------------------------------------

    def user_out(row: User) -> dict:
        return {"id": row.id, "email": row.email, "name": row.name, "role": row.role,
                "status": row.status, "has_password": row.password_hash is not None}

    @router.post("/api/admin/users", status_code=201)
    async def add_user(request: Request, admin: str = Depends(require_admin)):
        body = await _body(request)
        email = accounts.normalize_email(body.get("email"))
        if email is None:
            return _error(400, "invalid_email", "That doesn't look like an email address.")
        hashed = None
        if body.get("password"):
            hashed = new_password(body.get("password"))
            if isinstance(hashed, JSONResponse):
                return hashed
        now = ctx.now()
        with ctx.sessions() as db:
            if accounts.user_by_email(db, email) is not None:
                return _error(409, "exists", "Someone with that email already has an account.")
            row = User(id=str(uuid.uuid4()), email=email, name=_text(body.get("name"), 200),
                       role="user", status="active", created_at=now, password_hash=hashed)
            db.add(row)
            db.flush()
            out = {"user": user_out(row)}
            if hashed is None:
                out["link"] = set_password_link(db, admin, email)
            log_event(db, admin, "add_user", row.id,
                      {"with_password": hashed is not None}, now=now)
            try:
                db.commit()
            except sa.exc.IntegrityError:
                db.rollback()
                return _error(409, "exists", "Someone with that email already has an account.")
        return JSONResponse(out, status_code=201, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/users/{user_id}/password")
    async def reset_password(user_id: str, request: Request,
                             admin: str = Depends(require_admin)):
        """Give someone a new password, or a set-password link to send them.
        Either way their devices are signed out."""
        body = await _body(request)
        hashed = None
        if body.get("password"):
            hashed = new_password(body.get("password"))
            if isinstance(hashed, JSONResponse):
                return hashed
        now = ctx.now()
        with ctx.sessions() as db:
            row = db.get(User, user_id)
            if row is None:
                return _error(404, "not_found", "no such user")
            if row.id == admin:
                return _error(409, "self_action", "Change your own password in Settings.")
            out: dict = {}
            if hashed is not None:
                row.password_hash = hashed
            else:
                out["link"] = set_password_link(db, admin, row.email)
            delete_user_sessions(db, row.id)
            log_event(db, admin, "reset_password", row.id,
                      {"with_password": hashed is not None}, now=now)
            db.commit()
            out["user"] = user_out(row)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    # --- the admin: the AI provider for everyone ----------------------------

    from lore.central import keys as K

    @router.get("/api/admin/provider")
    def get_provider(_admin: str = Depends(require_admin)):
        return JSONResponse({"provider": key_service.server_provider()},
                            headers={"Cache-Control": "no-store"})

    @router.put("/api/admin/provider")
    async def put_provider(request: Request, admin: str = Depends(require_admin)):
        body = await _body(request)
        provider = body.get("provider")
        try:
            saved = key_service.set_server_provider(
                admin, provider if isinstance(provider, str) else "",
                key=body.get("key") if isinstance(body.get("key"), str) else None,
                model=body.get("model") if isinstance(body.get("model"), str) else None,
                fast_model=(body.get("fast_model") if isinstance(body.get("fast_model"), str)
                            else None),
                base_url=body.get("base_url") if isinstance(body.get("base_url"), str) else None,
                clear_key=body.get("clear_key") is True)
        except K.KeyFormatError:
            return _error(400, "key_format", "That doesn't look like an API key for that"
                                             " provider. Copy the whole key and paste it again.")
        except K.ServerProviderError as exc:
            return _error(400, "bad_request", str(exc))
        with ctx.sessions() as db:
            log_event(db, admin, "set_provider", detail={"provider": saved["provider"]},
                      now=ctx.now())
            db.commit()
        return JSONResponse({"provider": saved}, headers={"Cache-Control": "no-store"})

    @router.delete("/api/admin/provider")
    def delete_provider(admin: str = Depends(require_admin)):
        removed = key_service.clear_server_provider()
        if removed:
            with ctx.sessions() as db:
                log_event(db, admin, "clear_provider", now=ctx.now())
                db.commit()
        return JSONResponse({"provider": None, "removed": removed},
                            headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/provider/test")
    async def test_provider(request: Request, admin: str = Depends(require_admin)):
        """Test connection for a local model: one tiny structured call.
        {ok, model, seconds} or {ok: false, message} in plain words."""
        from lore.inference.providers.openai_compatible import OpenAICompatibleTextModel
        from lore.web.local_model import PING_PROMPT, TEST_TIMEOUT_S, _Ping, plain_error
        with ctx.sessions() as db:
            allowed = ratelimit.allow(db, "provider_test", admin, PROVIDER_TESTS_PER_HOUR,
                                      ctx.now())
            db.commit()
        if not allowed:
            return _error(429, "rate_limited", "Too many tests. Wait a while.")
        body = await _body(request)
        base_url, model = _text(body.get("base_url"), 500), _text(body.get("model"), 200)
        if not base_url or not base_url.startswith(("http://", "https://")):
            return _error(400, "bad_request", "The server address starts with http:// or"
                                              " https://.")
        if not model:
            return _error(400, "bad_request", "Name the model, as the server knows it.")
        key = _text(body.get("key"), 500)
        if key is None:  # the saved key, when testing the saved server
            saved = key_service.server_provider()
            if saved and saved["provider"] == "openai_compatible" and saved["has_key"]:
                with ctx.sessions() as db:
                    creds = key_service._server_credentials(db)
                key = None if creds is None or creds.key == "no-key" else creds.key
        client = OpenAICompatibleTextModel(key, base_url=base_url, model=model,
                                           timeout=TEST_TIMEOUT_S, transport=transport)
        started = time.monotonic()
        try:
            await client.generate_json(PING_PROMPT, _Ping)
        except Exception as exc:  # noqa: BLE001 - every failure is shown in words
            return JSONResponse({"ok": False, "message": plain_error(exc, base_url, model)},
                                headers={"Cache-Control": "no-store"})
        finally:
            await client.aclose()
        return JSONResponse({"ok": True, "model": model,
                             "seconds": round(time.monotonic() - started, 1)},
                            headers={"Cache-Control": "no-store"})

    return router
