"""A self-hosted model chosen in the web app's Settings (local mode only).

Routes (never mounted in multi mode, where the provider is refused anyway:
a server-side base URL that users steer would reach the server's network):
- GET /api/local-model: what runs the model steps now (`active`: provider,
  model, base URL, and `source`: "saved" in the app or "lore.toml"), the
  saved local model if any (`saved`, with `has_key`, never the key), what
  lore.toml says (`file`), and the default address.
- POST /api/local-model/test {base_url, model, key?}: one tiny structured
  call; {ok: true, seconds} or {ok: false, message} in plain words. Without
  a key in the body the saved one (if any) is used.
- PUT /api/local-model {base_url, model, fast_model?, key?, clear_key?}:
  save it; from then on it overrides lore.toml's [lore.llm] and runs every
  model step (lore.config.with_saved_llm). A key goes to
  ~/.config/lore/llm.env (mode 600), where lore.toml's setup reads it too.
- DELETE /api/local-model: forget it; lore.toml applies again as written.

The local app answers only its own page on this machine (web.security
OriginGuard), so no other page can point LORE at another server.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lore.config import LlmConfig, LoreConfig, read_saved_llm, remove_saved_llm, write_saved_llm
from lore.inference.gemini import GeminiKeyFileError
from lore.inference.providers import factory
from lore.inference.providers.base import (
    OPENAI_COMPATIBLE,
    ProviderError,
    ProviderKeyError,
    ProviderOutputError,
    ProviderTransportError,
)

DEFAULT_BASE_URL = "http://localhost:8080/v1"
# A first request may load the model into memory: allow for it, but not forever.
TEST_TIMEOUT_S = 180.0
KEY_VARIABLE = factory.KEY_VARIABLES[OPENAI_COMPATIBLE]


class _Ping(BaseModel):
    ok: bool


PING_PROMPT = 'This is a connection test. Reply with the JSON object {"ok": true}.'


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _key_path():
    return factory.key_file(OPENAI_COMPATIBLE)


def saved_key() -> str | None:
    try:
        return factory.local_key(OPENAI_COMPATIBLE, env={})
    except (GeminiKeyFileError, OSError):
        return None


def write_key(key: str | None) -> None:
    """Store the key in ~/.config/lore/llm.env (mode 600), or remove it (None)."""
    path = _key_path()
    if key is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{KEY_VARIABLE}={key}\n")
    os.chmod(path, 0o600)


def _describe(llm: LlmConfig) -> dict:
    return {"provider": llm.provider, "model": llm.model or None,
            "fast_model": llm.fast_model or None, "base_url": llm.base_url or None}


def state(config: LoreConfig) -> dict:
    saved = read_saved_llm(config.data_dir)
    file = config.file_config.llm
    active = _describe(saved if saved is not None else file)
    active["source"] = "saved" if saved is not None else "lore.toml"
    return {
        "active": active,
        "saved": None if saved is None else {**_describe(saved), "has_key": bool(saved_key())},
        "file": _describe(file),
        "default_base_url": DEFAULT_BASE_URL,
    }


def plain_error(exc: BaseException, base_url: str, model: str) -> str:
    """What went wrong, in words a reader can act on."""
    if isinstance(exc, ProviderTransportError):
        return (f"Couldn't reach a server at {base_url}. Check the address and that the"
                " server is running.")
    if isinstance(exc, ProviderKeyError):
        return "The server refused the key. Check it, or leave it empty if the server needs none."
    if isinstance(exc, ProviderOutputError):
        return (f"The server answered, but {model} didn't return the JSON LORE asked for."
                " A larger or instruction-tuned model may do better.")
    if isinstance(exc, ProviderError):
        return f"The server said: {str(exc)}"
    return f"The test failed: {type(exc).__name__}: {exc}"


def _body(raw: dict) -> tuple[str, str, str] | JSONResponse:
    base_url = raw.get("base_url")
    model = raw.get("model")
    fast = raw.get("fast_model") or ""
    if not isinstance(base_url, str) or not base_url.strip().startswith(("http://", "https://")):
        return _error(400, "bad_request", "The server address starts with http:// or https://,"
                                          f" for example {DEFAULT_BASE_URL}.")
    if not isinstance(model, str) or not model.strip():
        return _error(400, "bad_request", "Name the model, as the server knows it.")
    if not isinstance(fast, str):
        return _error(400, "bad_request", "The model for short steps is a name.")
    return base_url.strip(), model.strip(), fast.strip()


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def build_local_model_router(config_of: Callable[[Request], LoreConfig], *,
                             transport=None) -> APIRouter:
    """`config_of(request)`: the local library's configuration. `transport`
    is the HTTP transport the test call uses (tests fake it)."""
    router = APIRouter()

    @router.get("/api/local-model")
    def get_local_model(request: Request):
        return JSONResponse(state(config_of(request)), headers={"Cache-Control": "no-store"})

    @router.post("/api/local-model/test")
    async def test_local_model(request: Request):
        from lore.inference.providers.openai_compatible import OpenAICompatibleTextModel
        raw = await _json(request)
        parsed = _body(raw)
        if isinstance(parsed, JSONResponse):
            return parsed
        base_url, model, _fast = parsed
        key = raw.get("key") if isinstance(raw.get("key"), str) and raw.get("key") else saved_key()
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

    @router.put("/api/local-model")
    async def put_local_model(request: Request):
        raw = await _json(request)
        parsed = _body(raw)
        if isinstance(parsed, JSONResponse):
            return parsed
        base_url, model, fast = parsed
        config = config_of(request)
        try:
            write_saved_llm(config.data_dir, base_url, model, fast)
        except ValueError as exc:
            return _error(400, "bad_request", str(exc))
        key = raw.get("key")
        if isinstance(key, str) and key.strip():
            write_key(key.strip())
        elif raw.get("clear_key") is True:
            write_key(None)
        return JSONResponse(state(config_of(request)), headers={"Cache-Control": "no-store"})

    @router.delete("/api/local-model")
    def delete_local_model(request: Request):
        config = config_of(request)
        remove_saved_llm(config.data_dir)
        return JSONResponse(state(config_of(request)), headers={"Cache-Control": "no-store"})

    return router
