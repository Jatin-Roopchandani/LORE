"""Build the auth context for `create_app` in multi mode."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from lore.auth.current import AuthContext
from lore.auth.settings import AuthSettings
from lore.central.db import database_url, make_engine, make_session_factory
from lore.config import LoreConfig


def build_context(base: LoreConfig, *, auth: AuthSettings | None = None,
                  mailer=None, providers=None,
                  central_sessions: sessionmaker | None = None,
                  clock: Callable[[], datetime] | None = None) -> AuthContext:
    settings = auth or AuthSettings.from_env()  # raises AuthConfigError: no start
    sessions = central_sessions or make_session_factory(
        make_engine(database_url(base)))
    return AuthContext(
        settings=settings, sessions=sessions, mailer=mailer,
        providers=providers or {},
        now=clock or (lambda: datetime.now(timezone.utc)))
