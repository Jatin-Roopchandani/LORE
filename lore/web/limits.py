"""Request body limits (security review, release step 4).

A pure-ASGI middleware, so streamed answers are not buffered. Every request
with a body is capped, by its Content-Length when it gives one (refused
before a byte is read) and by the bytes actually received (a missing or
lying header changes nothing). The body is read up to the limit, then
handed on unchanged. Past the limit: 413 `too_large`.

Limits: IMPORT_BYTES for POST /api/chats (one chat imported from the
browser: its answer, capped at 1 MB when stored, plus its events), and
DEFAULT_BYTES for everything else (questions, names, settings, keys: all
small).

Browser upload (release step 5), POST /api/books/upload, has its own limit,
UPLOAD_BYTES: the 50 MB book plus room for the multipart framing. That body
is STREAMED, not buffered: the route writes it to a file as it arrives, so
the middleware only counts. Past the limit `receive` raises TooLarge, which
the route turns into the same 413 (and a Content-Length over the limit is
still refused before a byte is read).
"""
from __future__ import annotations

import json

IMPORT_BYTES = 2_000_000
DEFAULT_BYTES = 256 * 1024
UPLOAD_FILE_BYTES = 50 * 1024 * 1024  # the book itself
UPLOAD_BYTES = UPLOAD_FILE_BYTES + 64 * 1024  # plus the form's framing and world
UPLOAD_PATH = "/api/books/upload"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def limit_for(method: str, path: str) -> int:
    if method == "POST" and path.rstrip("/") == "/api/chats":
        return IMPORT_BYTES
    if streamed(method, path):
        return UPLOAD_BYTES
    return DEFAULT_BYTES


def streamed(method: str, path: str) -> bool:
    """The routes whose body is counted as it passes, never held in memory."""
    return method == "POST" and path.rstrip("/") == UPLOAD_PATH


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
        if streamed(scope["method"], scope.get("path", "")):
            return await self.app(scope, _counting(receive, limit), send)
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


def _counting(receive, limit: int):
    """`receive`, raising TooLarge once more than `limit` body bytes came."""
    seen = 0

    async def counted():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise TooLarge(limit)
        return message

    return counted
