"""The signed-in user's own model-provider keys, and the gate they put on model calls.

Multi mode only. Routes (every PUT/DELETE is behind the app-wide CSRF guard):
- GET /api/me/key: the ACTIVE provider's {provider, present, last4, added_at,
  last_ok_at, last_error}, never the key.
- GET /api/me/providers: every provider (gemini, anthropic, openai): connected,
  last4, the models its key offers, the chosen model(s), which is active, and
  the daily token cap.
- PUT /api/me/key {provider, key}: format check, then one cheap check (each
  SDK's models list; no generation), then stored sealed; returns the models.
  `provider` defaults to gemini. 10 a user an hour, every attempt counted.
- PUT /api/me/provider {provider, model?, fast_model?}: switch provider/model.
- DELETE /api/me/key?provider=: remove one provider's key (default: active).
- GET /api/me/usage: today's requests (Gemini's ~1,000), plus tokens today and
  this month with an estimated cost (Claude prices), and the token cap.
- PUT /api/me/token-cap {cap}: the optional daily token cap (null: off).

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
from lore.inference.providers.base import GEMINI, LABELS, PROVIDERS

log = logging.getLogger("lore.keys")

PUTS_PER_USER_PER_HOUR = 10

KEY_REQUIRED = {"code": "key_required",
                "message": "Connect your AI provider to ask questions and process books."}
KEY_INVALID = {"code": "key_invalid",
               "message": "Your AI provider refused your API key. Replace it in Settings."}
KEY_UNREADABLE = {"code": "key_invalid",
                  "message": "Your saved key can't be used any more. Add it again in"
                             " Settings."}

# Where each provider's keys are made, for the error text.
KEY_PAGES = {"gemini": "Google AI Studio", "anthropic": "the Claude Console",
             "openai": "the OpenAI dashboard"}


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

    def body_of(raw: bytes) -> dict:
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = None
        return body if isinstance(body, dict) else {}

    @router.get("/api/me/providers")
    def get_providers(request: Request):
        return JSONResponse(keys.providers(user_id(request)),
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
        body = body_of(await request.body())
        provider = body.get("provider") or GEMINI
        if provider not in PROVIDERS:
            return _error(400, "bad_request", "Choose Gemini, Claude or OpenAI.")
        name = LABELS[provider]
        try:
            outcome = keys.save(uid, body.get("key"), provider)
        except K.KeyFormatError:
            return _error(400, "key_format",
                          f"That doesn't look like a {name} API key. Copy the whole"
                          f" key from {KEY_PAGES[provider]} and paste it again.")
        if not outcome.stored:
            return _error(400, "key_invalid",
                          f"{name} didn't accept that key. Check you copied all of it,"
                          f" or create a new key in {KEY_PAGES[provider]}.")
        status = keys.status(uid, provider)
        status["check"] = outcome.check
        status["models"] = list(outcome.models)
        status["active"] = keys.active_provider(uid)
        if outcome.check != K.CHECK_OK:
            status["warning"] = (f"Saved, but {name} couldn't confirm the key just now"
                                 " (it may be busy or out of allowance). It"
                                 " will be checked on your next question.")
        return JSONResponse(status, headers={"Cache-Control": "no-store"})

    @router.put("/api/me/provider")
    async def put_provider(request: Request):
        uid = user_id(request)
        body = body_of(await request.body())
        provider = body.get("provider")
        model, fast = body.get("model"), body.get("fast_model")
        if (not isinstance(provider, str)
                or not all(v is None or isinstance(v, str) for v in (model, fast))):
            return _error(400, "bad_request", "Choose a provider and a model.")
        try:
            out = keys.choose(uid, provider, model or None, fast)
        except K.ProviderChoiceError as exc:
            return _error(400, "bad_request", str(exc).capitalize() + ".")
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @router.delete("/api/me/key")
    def delete_key(request: Request, provider: str | None = None):
        if provider is not None and provider not in PROVIDERS:
            return _error(400, "bad_request", "Choose Gemini, Claude or OpenAI.")
        return JSONResponse({"deleted": keys.remove(user_id(request), provider)},
                            headers={"Cache-Control": "no-store"})

    @router.get("/api/me/usage")
    def usage(request: Request):
        uid = user_id(request)
        out = keys.usage_today(uid)
        out["provider"] = keys.active_provider(uid)
        out["tokens"] = keys.usage_summary(uid)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @router.put("/api/me/token-cap")
    async def put_token_cap(request: Request):
        uid = user_id(request)
        cap = body_of(await request.body()).get("cap")
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int)
                                or cap < 0 or cap > 10**12):
            return _error(400, "bad_request", "A token cap is a whole number, or off.")
        keys.set_token_cap(uid, cap)
        return JSONResponse(keys.usage_summary(uid), headers={"Cache-Control": "no-store"})

    return router


def key_error(user: str, keys: K.KeyService) -> dict | None:
    """The `key_required` error when this user has no key, else None."""
    return None if keys.has_key(user) else {"error": dict(KEY_REQUIRED)}


def with_user_key(user: str, keys: K.KeyService,
                  search: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
    """`search`, run with `user`'s own key bound and their requests counted."""

    async def run(config, question, **kwargs):
        spent: list[str] = []
        try:
            # The user's active provider, key and model, their usage counted.
            scope = keys.scope(user, on_request=spent.append)
        except KeyRequired:
            return {"error": dict(KEY_REQUIRED)}
        except K.KeyDecryptError:
            log.warning("a stored key did not decrypt (user %s)", user)
            return {"error": dict(KEY_UNREADABLE)}
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
        scope = keys.scope(user, on_request=spent.append if spent is not None else None)
    except (KeyRequired, K.KeyDecryptError):
        scope = KeyScope(user, None, provider=keys.active_provider(user))
    with key_scope.bound(scope):
        yield scope
    if scope.rejected:
        keys.mark_rejected(user)
