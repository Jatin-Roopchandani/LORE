"""What a multi-mode user may see of LORE's internals: nothing (release step 6).

In multi mode no response, stream event or error carries a model name, a
prompt, a provider's raw error, a file path, a stack trace or a key fragment.

- **Errors** keep their `code` (the UI decides its wording by code) but their
  `message` is ours: a fixed sentence per code, or, for the codes whose
  message we write ourselves from the user's own input ("no world 'Dune'"),
  that message scrubbed. A code we do not know is `internal`. Every error
  carries the request id (`request_id`), and the generic message quotes it:
  "Something went wrong. Reference: <id>".
- **Exceptions** become that generic 500 (`PublicErrors`, a pure-ASGI
  middleware, so the SSE stream is not buffered); the traceback goes to the
  server log only (redacted, lore.auth.redact).
- **Stream events**: an `error` event is mapped as above; an `answer` event
  loses its model names; every other event's text is scrubbed.

Local mode is unchanged: one user, on their own machine, sees everything.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable

from lore import tracing

log = logging.getLogger("lore.web")

GENERIC = "Something went wrong. Reference: {rid}"

# The message is fixed: the server's own text may name a model, a provider's
# reply, a path, or the ledger's per-model counts.
FIXED = {
    "internal": GENERIC,
    "server_error": GENERIC,
    "upstream_unavailable": "The answer service is busy right now. Try again in a minute.",
    "timeout": "That took too long and was stopped. Try a narrower question.",
    "budget_exhausted": "Today's question allowance is used up. It resets at midnight Pacific.",
    "model_refused": "The AI model declined to answer this. Try asking it another way.",
    "cypher_rejected": "That query isn't allowed.",
    "mail_failed": "The email could not be sent. Nothing was changed.",
    "cancelled": "The question was cancelled.",
    "not_a_book": "Only EPUB and PDF books with readable text can be added.",
    "too_complex": "This book is too large or complex to read safely. Try another copy.",
    "encrypted": "This book is protected (DRM or a password), so LORE can't read it.",
    "unsafe_file": "This file doesn't look like a safe EPUB, so it wasn't added.",
    "too_many_pages": "This PDF has too many pages.",
    "unauthorized": "Please sign in.",
    "forbidden": "That isn't allowed.",
    "method_not_allowed": "That isn't allowed.",
    "invalid_request": "That request couldn't be read.",
    "unsupported": "That request couldn't be read.",
    "account_deleted": "This account was deleted.",
}

# Codes whose message LORE writes itself from the request ("no world 'x'",
# "a book can be at most 50 MB"): passed on, scrubbed. The fallback is used
# when the message is empty.
PASS = {
    "not_found": "That isn't here any more.",
    "busy": "LORE is still working on your previous request.",
    "rate_limited": "Too many requests. Please wait a moment and try again.",
    "daily_limit": "You've reached today's limit. Try again tomorrow.",
    "not_resumable": "That book can't be continued right now.",
    "unknown_world": "There is no such world.",
    "invalid_world": "That world name can't be used.",
    "world_required": "Choose a world first.",
    "invalid_position": "That reading position can't be saved.",
    "not_empty": "This world still has books.",
    "exists": "That already exists.",
    "no_graph": "There's no graph yet. Process a book first.",
    "too_many_books": "Your library is full.",
    "already_added": "This book is already in your library.",
    "already_invited": "This person was already invited.",
    "bad_upload": "The upload couldn't be read.",
    "too_large": "That's too large.",
    "history_full": "Your history is full. Delete some chats first.",
    "invalid_email": "That doesn't look like an email address.",
    "key_required": "Connect your AI provider to ask questions and process books.",
    "key_invalid": "Your AI provider refused your API key. Replace it in Settings.",
    "key_format": "That doesn't look like an API key for this provider.",
    "bad_request": "That request couldn't be read.",
    "confirm_mismatch": "The confirmation didn't match.",
    "self_action": "You can't do that to your own account.",
    "current_session": "Use Sign out to sign this device out.",
}

# FastAPI's {"detail": ...} errors (HTTPException, validation) by status.
BY_STATUS = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "busy", 413: "too_large", 415: "unsupported",
    422: "invalid_request", 429: "rate_limited",
}

# Model names: gemini-2.5-flash, gemma-3-27b-it, models/..., claude-sonnet-5-5,
# gpt-9-mini, o7-mini, and whatever the configuration names (lore.config),
# added by `known_models`.
_MODEL = re.compile(r"\b(?:models/)?(?:(?:gemini|gemma)-[0-9][\w.\-]*"
                    r"|claude-[a-z]+-[0-9][\w.\-]*|gpt-[0-9][\w.\-]*|o[0-9]+-[a-z][\w.\-]*)",
                    re.IGNORECASE)
_PATH = re.compile(r"(?<![\w/:])/(?:[\w.\-]+/)+[\w.\-]*")
_TRACE = re.compile(r"Traceback \(most recent call last\).*", re.DOTALL)
_FRAME = re.compile(r'File "[^"]*", line \d+')
_extra_models: set[str] = set()


def known_models(names: Iterable[str]) -> None:
    """Also scrub these configured model names (any shape)."""
    for name in names:
        if isinstance(name, str) and len(name) >= 4:
            _extra_models.add(name)


def config_models(config) -> list[str]:
    """Every model name a LoreConfig mentions (keys containing "model")."""
    found: list[str] = []

    def walk(value, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and "model" in key.lower():
            found.append(value)

    try:
        walk(config.model_dump())
    except Exception:  # noqa: BLE001 -- best effort; the regex still applies
        pass
    return found


def scrub_models(text: str) -> str:
    for name in sorted(_extra_models, key=len, reverse=True):
        if name in text:
            text = text.replace(name, "the model")
    return _MODEL.sub("the model", text)


def scrub_text(text: str) -> str:
    """No key, model name, stack trace, source location or file path."""
    from lore.auth.redact import redact_keys
    text = redact_keys(text)
    text = _TRACE.sub("", text)
    text = _FRAME.sub("", text)
    text = _PATH.sub("[path]", text)
    return scrub_models(text).strip()


def _deep(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_deep(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _deep(v, fn) for k, v in value.items()}
    return value


def public_error(error: object, rid: str | None = None) -> dict:
    """`error` ({code, message, ...}) as a user may see it, with the request id."""
    rid = rid or tracing.current_request_id() or "unknown"
    err = error if isinstance(error, dict) else {}
    code = err.get("code") if isinstance(err.get("code"), str) else "internal"
    if code in PASS:
        raw = err.get("message")
        message = scrub_text(raw) if isinstance(raw, str) else ""
        message = message or PASS[code]
    elif code in FIXED:
        message = FIXED[code].format(rid=rid)
    else:
        code, message = "internal", GENERIC.format(rid=rid)
    out = {"code": code, "message": message, "request_id": rid}
    for extra in ("retry_after", "book_id", "resumable_at"):
        if extra in err and isinstance(err[extra], (int, float, str)):
            out[extra] = err[extra]
    return out


def public_result(result: object) -> object:
    """An answer without model names (`model`, `selection_model`)."""
    if not isinstance(result, dict):
        return result
    out = _deep(result, scrub_models)
    for key in ("model", "selection_model"):
        if key in out:
            out[key] = None
    return out


def public_event(kind: str, payload: dict, rid: str | None = None) -> dict:
    """One stream event as a multi-mode user may see it."""
    if kind == "error":
        return {**payload, "error": public_error(payload.get("error"), rid)}
    if kind == "answer":
        return {**payload, "result": public_result(payload.get("result"))}
    return _deep(payload, scrub_text)


def public_body(status: int, body: object) -> object | None:
    """A JSON body rewritten for a user, or None when it needs no change."""
    if not isinstance(body, dict):
        return None
    rid = tracing.current_request_id()
    if isinstance(body.get("error"), dict):
        return {**body, "error": public_error(body["error"], rid)}
    if status >= 400 and "detail" in body:
        detail = body["detail"]
        code = BY_STATUS.get(status, "internal" if status >= 500 else "bad_request")
        # A validation error lists the request's own values: never echoed.
        text = detail if isinstance(detail, str) and status != 422 else ""
        err = public_error({"code": code, "message": text}, rid)
        return {"error": err, "detail": err["message"]}
    return None


def _json_type(headers) -> bool:
    for name, value in headers:
        if name.lower() == b"content-type":
            return value.split(b";")[0].strip().lower() == b"application/json"
    return False


class PublicErrors:
    """Pure ASGI: an unhandled exception is a friendly 500 with the request id
    (never a traceback), and every JSON body with an error is rewritten by
    `public_body`. Install it INSIDE `RequestId`, so the id is bound."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False
        held: dict | None = None
        chunks: list[bytes] = []

        async def filtered(message):
            nonlocal started, held
            if message["type"] == "http.response.start":
                started = True
                if _json_type(message.get("headers", [])):
                    held = message
                    return
                return await send(message)
            if message["type"] == "http.response.body" and held is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                status = held["status"]
                body = b"".join(chunks)
                try:
                    rewritten = public_body(status, json.loads(body)) if body else None
                except ValueError:
                    rewritten = None
                if rewritten is not None:
                    body = json.dumps(rewritten).encode()
                headers = [(k, v) for k, v in held.get("headers", [])
                           if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**held, "headers": headers})
                return await send({"type": "http.response.body", "body": body})
            return await send(message)

        try:
            await self.app(scope, receive, filtered)
        except Exception:
            log.exception("unhandled error")
            if started and held is None:
                raise  # a stream was under way: nothing friendly can be sent now
            err = public_error({"code": "internal"})
            body = json.dumps({"error": err, "detail": err["message"]}).encode()
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
