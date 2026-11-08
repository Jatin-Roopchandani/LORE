"""The signed-in user's MCP access tokens (multi mode).

- GET /api/me/mcp-tokens: the active tokens (name, prefix, created, last
  used), never a token or its hash.
- POST /api/me/mcp-tokens {name}: a new token, returned ONCE in the body
  (201, no-store). At most MAX_TOKENS_PER_USER active tokens (409
  `too_many_tokens`), and CREATES_PER_HOUR attempts an hour (429).
- DELETE /api/me/mcp-tokens/{id}: revoke one; someone else's is 404.

Session cookie only: an `Authorization: Bearer` header is never read here,
so a token cannot mint or revoke tokens. POST and DELETE carry the CSRF token
like every mutating route (the app-wide guard).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lore.auth import ratelimit
from lore.auth.current import AuthContext, session_user
from lore.central import mcp_tokens as T

NO_STORE = {"Cache-Control": "no-store"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status, headers=NO_STORE)


def build_mcp_token_router(ctx: AuthContext) -> APIRouter:
    router = APIRouter()

    def user_id(request: Request) -> str | None:
        user = session_user(request)
        return None if user is None else user.id

    def unauthorized() -> JSONResponse:
        return _error(401, "unauthorized", "sign in required")

    @router.get("/api/me/mcp-tokens")
    def list_tokens(request: Request):
        uid = user_id(request)
        if uid is None:
            return unauthorized()
        with ctx.sessions() as db:
            tokens = T.list_for(db, uid)
        return JSONResponse({"tokens": tokens, "max_tokens": T.MAX_TOKENS_PER_USER},
                            headers=NO_STORE)

    @router.post("/api/me/mcp-tokens")
    async def create_token(request: Request):
        uid = user_id(request)
        if uid is None:
            return unauthorized()
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        name = body.get("name") if isinstance(body, dict) else None
        with ctx.sessions() as db:
            if not ratelimit.allow(db, "mcp_token", uid, T.CREATES_PER_HOUR, ctx.now()):
                db.commit()
                return _error(429, "rate_limited",
                              "Too many new tokens this hour. Please try again later.")
            try:
                row, raw = T.create(db, uid, name, ctx.now())
            except T.BadName as exc:
                db.commit()  # the attempt still counts
                return _error(400, "bad_request", str(exc).capitalize() + ".")
            except T.TooManyTokens:
                db.commit()
                return _error(409, "too_many_tokens",
                              f"You have {T.MAX_TOKENS_PER_USER} tokens, the most allowed."
                              " Revoke one first.")
            out = {**T.public(row), "token": raw}
            db.commit()
        return JSONResponse(out, status_code=201, headers=NO_STORE)

    @router.delete("/api/me/mcp-tokens/{token_id}")
    def revoke_token(token_id: str, request: Request):
        uid = user_id(request)
        if uid is None:
            return unauthorized()
        with ctx.sessions() as db:
            gone = T.revoke(db, uid, token_id[:64], ctx.now())
            db.commit()
        if not gone:
            return _error(404, "not_found", "no such token")
        return JSONResponse({"revoked": True}, headers=NO_STORE)

    return router
