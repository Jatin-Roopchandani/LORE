"""lore ui has no authentication, so: bind loopback only, and serve only
requests addressed to this server (Host) from its own page (Origin). Same
reasoning as the MCP server's DNS-rebinding protection: any page in the
user's browser can otherwise reach a localhost port and spend Gemini."""
from __future__ import annotations

import json

from lore.mcp.server import NonLoopbackHost, _check_loopback

__all__ = ["NonLoopbackHost", "OriginGuard", "allowed_hosts", "allowed_origins",
           "check_loopback"]

VITE_DEV_PORT = 5173


def check_loopback(host: str) -> None:
    _check_loopback(host)


def _shown(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def allowed_hosts(host: str, port: int) -> set[str]:
    return {f"{_shown(host)}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"}


def allowed_origins(host: str, port: int, dev: bool) -> set[str]:
    origins = {f"http://{h}" for h in allowed_hosts(host, port)}
    if dev:
        origins |= {f"http://localhost:{VITE_DEV_PORT}",
                    f"http://127.0.0.1:{VITE_DEV_PORT}"}
    return origins


class OriginGuard:
    """Pure ASGI middleware (not BaseHTTPMiddleware: it must not buffer the
    SSE stream or hide client disconnects from it)."""

    def __init__(self, app, hosts: set[str], origins: set[str]) -> None:
        self.app, self.hosts, self.origins = app, hosts, origins

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            host, origin = headers.get("host"), headers.get("origin")
            if host not in self.hosts:
                return await _forbidden(send, "this server only answers requests"
                                        " addressed to it on this machine")
            if origin is not None and origin not in self.origins:
                return await _forbidden(send, "requests from other web pages are"
                                        " refused")
        await self.app(scope, receive, send)


async def _forbidden(send, message: str) -> None:
    body = json.dumps({"error": {"code": "forbidden", "message": message}}).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
