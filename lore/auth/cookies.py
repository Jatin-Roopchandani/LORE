"""Cookie headers, spelled out so every flag is visible in one place."""
from __future__ import annotations

from lore.auth.settings import AuthSettings

SESSION_COOKIE = "lore_session"
CSRF_COOKIE = "lore_csrf"
FLOW_COOKIE = "lore_oauth"  # the OAuth state and PKCE verifier, for one sign-in


def cookie_header(name: str, value: str, *, max_age: int, secure: bool,
                  http_only: bool, path: str = "/") -> tuple[bytes, bytes]:
    """A `Set-Cookie` header: SameSite=Lax always; HttpOnly and Secure as asked."""
    parts = [f"{name}={value}", f"Max-Age={max_age}", f"Path={path}", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return (b"set-cookie", "; ".join(parts).encode("latin-1"))


def session_headers(settings: AuthSettings, session_token: str, csrf: str,
                    max_age: int) -> list[tuple[bytes, bytes]]:
    return [
        cookie_header(SESSION_COOKIE, session_token, max_age=max_age,
                      secure=settings.cookie_secure, http_only=True),
        # Readable by the page's script (the client echoes it in a header).
        cookie_header(CSRF_COOKIE, csrf, max_age=max_age,
                      secure=settings.cookie_secure, http_only=False),
    ]


def cleared_headers(settings: AuthSettings,
                    *names: str) -> list[tuple[bytes, bytes]]:
    out = []
    for name in names or (SESSION_COOKIE, CSRF_COOKIE):
        out.append(cookie_header(name, "", max_age=0, secure=settings.cookie_secure,
                                 http_only=name != CSRF_COOKIE,
                                 path="/auth" if name == FLOW_COOKIE else "/"))
    return out
