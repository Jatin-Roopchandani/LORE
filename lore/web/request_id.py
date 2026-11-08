"""A request id for every HTTP request (release step 4).

A pure-ASGI middleware (so a streamed answer is not buffered): it makes a
UUID per request, binds it for the request's whole life (lore.tracing, so
every log line made while handling it carries it, and /api/ask keys its
trace by it), and returns it in the `X-Request-Id` response header. A
request id from the client is never trusted.
"""
from __future__ import annotations

from lore import tracing


class RequestId:
    def __init__(self, app) -> None:
        self.app = app
        tracing.install_request_id_logging()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = tracing.new_request_id()

        async def stamped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with tracing.request_id_bound(rid):
            await self.app(scope, receive, stamped)
