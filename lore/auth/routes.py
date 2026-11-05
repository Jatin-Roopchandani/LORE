"""The auth routes: sign-out and who-am-I here; sign-in routes are added to
the same router by the OAuth and magic-link modules."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from lore.auth.cookies import CSRF_COOKIE, SESSION_COOKIE, cleared_headers, session_headers
from lore.auth.current import AuthContext, session_user
from lore.auth.sessions import LIFETIME, delete_session
from lore.auth.tokens import csrf_token, same
from lore.auth.admin import build_admin_router
from lore.auth.signin import build_signin_router
from lore.central.models import User


def build_router(ctx: AuthContext) -> APIRouter:
    router = APIRouter()
    router.include_router(build_signin_router(ctx))
    router.include_router(build_admin_router(ctx))
    settings = ctx.settings

    @router.post("/auth/logout")
    def logout(request: Request):
        """Delete the session row and clear the cookies. CSRF-protected like
        every other POST (the guard in create_app), so a page cannot sign
        someone out."""
        with ctx.sessions() as db:
            delete_session(db, request.cookies.get(SESSION_COOKIE))
            db.commit()
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        for name, value in cleared_headers(settings):
            response.headers.append(name.decode(), value.decode())
        return response

    @router.post("/api/me/delete")
    async def delete_me(request: Request):
        """Delete my account and everything in it (lore.central.erase), once
        the body's `confirm` is my own email address, typed. CSRF-protected
        like every POST. Safe to repeat."""
        from lore.auth.accounts import normalize_email
        from lore.central.erase import erase_user
        user = session_user(request)
        if user is None:
            return JSONResponse({"error": {"code": "unauthorized",
                                           "message": "sign in required"}}, status_code=401)
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        typed = body.get("confirm") if isinstance(body, dict) else None
        with ctx.sessions() as db:
            row = db.get(User, user.id)
        if row is None or normalize_email(typed) != row.email:
            return JSONResponse({"error": {"code": "confirm_mismatch",
                                           "message": "type your email address to confirm"}},
                                status_code=400)
        done = erase_user(ctx.sessions, ctx.data_root, user.id, now=ctx.now())
        response = JSONResponse({"deleted": True, "library_removed": done.library_removed},
                                headers={"Cache-Control": "no-store"})
        for name, value in cleared_headers(settings):
            response.headers.append(name.decode(), value.decode())
        return response

    @router.get("/api/me")
    def me(request: Request, response: Response):
        """The signed-in user. Also re-issues the readable CSRF cookie when the
        browser has lost it (the session cookie is still good)."""
        user = session_user(request)
        if user is None:
            return JSONResponse({"error": {"code": "unauthorized",
                                           "message": "sign in required"}},
                                status_code=401)
        with ctx.sessions() as db:
            row = db.get(User, user.id)
        token = request.cookies[SESSION_COOKIE]
        expected = csrf_token(settings.csrf_secret, token)
        have = request.cookies.get(CSRF_COOKIE)
        if have is None or not same(have, expected):
            name, value = session_headers(settings, token, expected,
                                          int(LIFETIME.total_seconds()))[1]
            response.headers.append(name.decode(), value.decode())
        response.headers["Cache-Control"] = "no-store"
        return {"user": {"id": row.id, "email": row.email, "name": row.name,
                         "role": row.role}}

    return router
