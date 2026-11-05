"""The signed-in user's own Gemini key, and the gate it puts on model calls.

Multi mode only. Routes (every PUT/DELETE is behind the app-wide CSRF guard):
- GET /api/me/key: {present, last4, added_at, last_ok_at, last_error}, never the key.
- PUT /api/me/key {key}: format check, then one cheap check request, then
  stored sealed. 10 a user an hour.
- DELETE /api/me/key.
- GET /api/me/usage: today's requests against the free tier's ~1,000.

`with_user_key` wraps the question path: it decrypts the asker's key, binds
it for the question (lore.inference.key_scope), and turns a missing or
refused key into `key_required` / `key_invalid`, never a stack trace.
"""
from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from lore.auth import ratelimit
from lore.auth.current import AuthContext, session_user
from lore.central import keys as K
from lore.inference import key_scope
from lore.inference.key_scope import KeyRequired, KeyScope

log = logging.getLogger("lore.keys")

PUTS_PER_USER_PER_HOUR = 10

KEY_REQUIRED = {"code": "key_required",
                "message": "Add your Gemini API key to ask questions and process books."}
KEY_INVALID = {"code": "key_invalid",
               "message": "Gemini refused your API key. Replace it in Account settings."}
KEY_UNREADABLE = {"code": "key_invalid",
                  "message": "Your saved key can't be used any more. Add it again in"
                             " Account settings."}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status, headers={"Cache-Control": "no-store"})


def build_key_router(ctx: AuthContext, keys: K.KeyService) -> APIRouter:
    router = APIRouter()

    def user_id(request: Request) -> str:
        user = session_user(request)
        if user is None:
            raise HTTPException(401, "sign in required")
        return user.id

    @router.get("/api/me/key")
    def get_key(request: Request):
        return JSONResponse(keys.status(user_id(request)),
                            headers={"Cache-Control": "no-store"})

    @router.put("/api/me/key")
    async def put_key(request: Request):
        uid = user_id(request)
        with ctx.sessions() as db:  # every attempt counts, well-formed or not
            allowed = ratelimit.allow(db, "key_put", uid, PUTS_PER_USER_PER_HOUR,
                                      ctx.now())
            db.commit()
        if not allowed:
            return _error(429, "rate_limited",
                          "Too many tries. Please wait a while and try again.")
        # Read by hand, not through a pydantic model: FastAPI's validation
        # error would echo the submitted value back.
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        text = body.get("key") if isinstance(body, dict) else None
        try:
            outcome = keys.save(uid, text)
        except K.KeyFormatError:
            return _error(400, "key_format",
                          "That doesn't look like a Gemini API key. Copy the whole"
                          " key from Google AI Studio and paste it again.")
        if not outcome.stored:
            return _error(400, "key_invalid",
                          "Gemini didn't accept that key. Check you copied all of it,"
                          " or create a new key in Google AI Studio.")
        status = keys.status(uid)
        status["check"] = outcome.check
        if outcome.check != K.CHECK_OK:
            status["warning"] = ("Saved, but Gemini couldn't confirm the key just now"
                                 " (it may be busy or out of today's allowance). It"
                                 " will be checked on your next question.")
        return JSONResponse(status, headers={"Cache-Control": "no-store"})

    @router.delete("/api/me/key")
    def delete_key(request: Request):
        return JSONResponse({"deleted": keys.remove(user_id(request))},
                            headers={"Cache-Control": "no-store"})

    @router.get("/api/me/usage")
    def usage(request: Request):
        return JSONResponse(keys.usage_today(user_id(request)),
                            headers={"Cache-Control": "no-store"})

    return router


def key_error(user: str, keys: K.KeyService) -> dict | None:
    """The `key_required` error when this user has no key, else None."""
    return None if keys.has_key(user) else {"error": dict(KEY_REQUIRED)}


def with_user_key(user: str, keys: K.KeyService,
                  search: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """`search`, run with `user`'s own key bound and their requests counted."""

    async def run(config, question, **kwargs):
        try:
            key = keys.key_for(user)
        except KeyRequired:
            return {"error": dict(KEY_REQUIRED)}
        except K.KeyDecryptError:
            log.warning("a stored key did not decrypt (user %s)", user)
            return {"error": dict(KEY_UNREADABLE)}
        spent: list[str] = []

        def on_request(model: str) -> None:
            spent.append(model)
            keys.record_request(user, model)

        scope = KeyScope(user, key, on_request=on_request)
        del key
        with key_scope.bound(scope):
            result = await search(config, question, **kwargs)
        if scope.rejected:
            # Whatever the search made of it (it reports a Gemini failure as
            # upstream_unavailable), the user's key was refused.
            keys.mark_rejected(user)
            return {"error": dict(KEY_INVALID)}
        if spent and not (isinstance(result, dict) and "error" in result):
            keys.mark_ok(user)  # a key saved unchecked has now worked
        return result

    return run


@contextlib.contextmanager
def user_key_bound(user: str, keys: K.KeyService,
                   spent: list[str] | None = None) -> Iterator[KeyScope]:
    """Bind `user`'s own key (an empty scope when they have none, or it no
    longer decrypts, so nothing can fall back to the server's key) around a
    synchronous model call, such as the entity intro. A refused key is marked,
    exactly as on the ask path. Each request's model is appended to `spent`
    when given (the intro's rate limit counts them)."""
    try:
        key = keys.key_for(user)
    except (KeyRequired, K.KeyDecryptError):
        key = None

    def on_request(model: str) -> None:
        if spent is not None:
            spent.append(model)
        keys.record_request(user, model)

    scope = KeyScope(user, key, on_request=on_request)
    del key
    with key_scope.bound(scope):
        yield scope
    if scope.rejected:
        keys.mark_rejected(user)
