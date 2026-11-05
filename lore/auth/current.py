"""The signed-in user of a request, and the middleware that re-sends cookies."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.orm import sessionmaker

from lore.auth.cookies import SESSION_COOKIE, session_headers
from lore.auth.sessions import LIFETIME, resolve
from lore.auth.settings import AuthSettings
from lore.auth.tokens import csrf_token
from lore.tenancy import CurrentUser

if TYPE_CHECKING:
    from lore.auth.mailer import Mailer
    from lore.auth.providers import Provider


@dataclass
class AuthContext:
    """Everything auth needs, hung on `app.state.auth` by `create_app`."""

    settings: AuthSettings
    sessions: sessionmaker
    mailer: Mailer
    providers: dict[str, Provider]
    now: Callable[[], datetime]


def session_user(request: Request) -> CurrentUser | None:
    """Cookie -> session row -> active user; None when any step fails.

    Resolved once per request (several dependencies ask). When the expiry
    slides, the new cookie is queued for `SessionRefresh` to send.
    """
    cached = getattr(request.state, "auth_user", False)
    if cached is not False:
        return cached
    user = None
    app = request.scope.get("app")
    ctx: AuthContext | None = getattr(getattr(app, "state", None), "auth", None)
    token = request.cookies.get(SESSION_COOKIE)
    if ctx is not None and token:
        with ctx.sessions() as db:
            found = resolve(db, token, ctx.now())
            db.commit()
        if found is not None:
            user = CurrentUser(found.user_id)
            if found.refreshed:
                request.state.session_refresh = token
                request.state.session_max_age = found.max_age
    request.state.auth_user = user
    return user


class SessionRefresh:
    """Pure ASGI middleware: when a request slid its session's expiry, add the
    refreshed cookies to the response. (Not BaseHTTPMiddleware: it would
    buffer the SSE stream.)"""

    def __init__(self, app, settings: AuthSettings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})

        async def send_with_cookies(message):
            if message["type"] == "http.response.start":
                token = scope["state"].get("session_refresh")
                if token:
                    csrf = csrf_token(self.settings.csrf_secret, token)
                    max_age = scope["state"].get("session_max_age",
                                                 int(LIFETIME.total_seconds()))
                    message = dict(message)
                    message["headers"] = [*message.get("headers", []),
                                          *session_headers(
                                              self.settings, token, csrf, max_age)]
            await send(message)

        await self.app(scope, receive, send_with_cookies)
