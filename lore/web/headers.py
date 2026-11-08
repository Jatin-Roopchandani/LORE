"""Security headers on every multi-mode response (release step 6, task 3).

A pure-ASGI middleware, outermost, so HTML pages, the API, static files and
even the origin guard's refusals all carry them. A header a route set itself
is kept (the sign-in redirects send `Referrer-Policy: no-referrer`, stricter,
because their URL can hold a token).

The Content-Security-Policy fits the built app (web/, Vite):
- scripts only from this origin, none inline: the theme is set before first
  paint by /theme-boot.js, a file, not an inline script;
- styles only from this origin: the theme blocks are a stylesheet file
  (themes-*.css), not an inline <style>; React's `style` props are set
  through the CSSOM, which a CSP does not govern;
- images and fonts from this origin and `data:` (Vite inlines a few small
  font files as data URIs);
- fetches only to this origin; no plugins, no <base>, forms post only here,
  and no page may frame LORE.
"""
from __future__ import annotations

CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
HSTS = "max-age=31536000; includeSubDomains"

HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode()),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"x-frame-options", b"DENY"),
)


class SecurityHeaders:
    def __init__(self, app, *, hsts: bool) -> None:
        self.app = app
        self.headers = HEADERS + (((b"strict-transport-security", HSTS.encode()),)
                                  if hsts else ())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.headers if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
