"""Build the auth context for `create_app` in multi mode."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from lore.auth.current import AuthContext
from lore.auth.mailer import ResendMailer, UnconfiguredMailer
from lore.auth.providers import default_providers
from lore.auth.redact import install_log_redaction
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
    if mailer is None:
        mailer = (ResendMailer(settings.resend_api_key, settings.mail_from)
                  if settings.resend_api_key else UnconfiguredMailer())
    if providers is None:
        providers = default_providers(settings.google, settings.github)
    install_log_redaction()
    return AuthContext(
        settings=settings, sessions=sessions, mailer=mailer, providers=providers,
        now=clock or (lambda: datetime.now(timezone.utc)), data_root=base.data_dir)
