"""The signed-in side of OAuth for MCP clients (multi mode; lore.mcp.oauth).

- GET /api/oauth/consent?req=: what the consent page shows (the client's name
  and where it sends the browser back to). 400 `invalid_request` for a
  missing, tampered or expired request.
- POST /api/oauth/consent {req, approve}: the reader's answer. Returns
  {redirect}: the client's registered redirect URI with a code for the
  signed-in user (or error=access_denied). The page then navigates there; a
  form post cannot, because the CSP's form-action allows only this origin.
- GET /api/me/mcp-connections: the connected apps (one per consent).
- DELETE /api/me/mcp-connections/{id}: disconnect one; every token it was
  given stops working. Someone else's is 404.

Session cookie only, like every /api route; POST and DELETE carry the CSRF
token (the app-wide guard).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lore.auth.current import AuthContext, session_user
from lore.mcp.oauth import ConsentError, LoreOAuthProvider

NO_STORE = {"Cache-Control": "no-store"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status, headers=NO_STORE)


def build_oauth_router(ctx: AuthContext, provider: LoreOAuthProvider) -> APIRouter:
    router = APIRouter()

    def unauthorized() -> JSONResponse:
        return _error(401, "unauthorized", "sign in required")

    def expired() -> JSONResponse:
        return _error(400, "invalid_request",
                      "This connection request has expired. Start connecting again from your app.")

    @router.get("/api/oauth/consent")
    def describe(request: Request, req: str = ""):
        if session_user(request) is None:
            return unauthorized()
        try:
            return JSONResponse(provider.describe(req), headers=NO_STORE)
        except ConsentError:
            return expired()

    @router.post("/api/oauth/consent")
    async def decide(request: Request):
        user = session_user(request)
        if user is None:
            return unauthorized()
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("approve"), bool):
            return _error(400, "bad_request", "Allow or cancel.")
        try:
            url = provider.decide(body.get("req"), user.id, body["approve"])
        except ConsentError:
            return expired()
        return JSONResponse({"redirect": url}, headers=NO_STORE)

    @router.get("/api/me/mcp-connections")
    def connections(request: Request):
        user = session_user(request)
        if user is None:
            return unauthorized()
        return JSONResponse({"connections": provider.connections(user.id)}, headers=NO_STORE)

    @router.delete("/api/me/mcp-connections/{grant_id}")
    def disconnect(grant_id: str, request: Request):
        user = session_user(request)
        if user is None:
            return unauthorized()
        if not provider.disconnect(user.id, grant_id):
            return _error(404, "not_found", "no such connection")
        return JSONResponse({"revoked": True}, headers=NO_STORE)

    return router
