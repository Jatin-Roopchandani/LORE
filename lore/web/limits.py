"""Request body limits (security review, release step 4).

A pure-ASGI middleware, so streamed answers are not buffered. Every request
with a body is capped, by its Content-Length when it gives one (refused
before a byte is read) and by the bytes actually received (a missing or
lying header changes nothing). The body is read up to the limit, then
handed on unchanged. Past the limit: 413 `too_large`.

Limits: IMPORT_BYTES for POST /api/chats (one chat imported from the
browser: its answer, capped at 1 MB when stored, plus its events), and
DEFAULT_BYTES for everything else (questions, names, settings, keys: all
small). Browser upload (release step 5) will need its own, larger route
limit here.
"""
from __future__ import annotations

import json

IMPORT_BYTES = 2_000_000
DEFAULT_BYTES = 256 * 1024
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def limit_for(method: str, path: str) -> int:
    if method == "POST" and path.rstrip("/") == "/api/chats":
        return IMPORT_BYTES
    return DEFAULT_BYTES


class TooLarge(Exception):
    pass


async def _refuse(send, limit: int) -> None:
    body = json.dumps({"error": {"code": "too_large",
                                 "message": f"the request is over {limit} bytes"}}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return await self.app(scope, receive, send)
        limit = limit_for(scope["method"], scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(value) > limit:
                        return await _refuse(send, limit)
                except ValueError:
                    return await _refuse(send, limit)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                chunks = None
                pending = message
                break
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                return await _refuse(send, limit)
            chunks.append(body)
            if not message.get("more_body"):
                pending = None
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                if chunks is None:
                    return pending
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()  # after the body: the disconnect, as before

        await self.app(scope, replay, send)
