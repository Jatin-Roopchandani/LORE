"""Build the auth context for `create_app` in multi mode."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from lore.auth.current import AuthContext
import os

from lore.auth.mailer import DevMailboxMailer, ResendMailer, UnconfiguredMailer
from lore.auth.providers import default_providers
from lore.auth.redact import install_log_redaction
from lore.auth.settings import AuthSettings
from lore.central.db import database_url, make_engine, make_session_factory
from lore.config import LoreConfig


def build_context(base: LoreConfig, *, auth: AuthSettings | None = None,
                  mailer=None, providers=None,
                  central_sessions: sessionmaker | None = None,
                  clock: Callable[[], datetime] | None = None) -> AuthContext:
    if auth is None:
        # Self-host: secrets not in the environment are made on first start
        # and kept in the data root (lore.selfhost); a no-op otherwise.
        from lore.selfhost import apply_secrets
        apply_secrets(base.data_dir)
    settings = auth or AuthSettings.from_env()  # raises AuthConfigError: no start
    sessions = central_sessions or make_session_factory(
        make_engine(database_url(base)))
    if mailer is None:
        if os.environ.get("LORE_MAIL_CONSOLE", "").strip().lower() in ("1", "true", "yes", "on"):
            # Local development: mail goes to <data root>/dev-mailbox.txt (localhost only).
            mailer = DevMailboxMailer(base.data_dir / "dev-mailbox.txt", settings.base_url)
        else:
            mailer = (ResendMailer(settings.resend_api_key, settings.mail_from,
                                   reply_to=settings.mail_reply_to)
                      if settings.resend_api_key else UnconfiguredMailer())
    if providers is None:
        providers = default_providers(settings.google, settings.github)
    install_log_redaction()
    return AuthContext(
        settings=settings, sessions=sessions, mailer=mailer, providers=providers,
        now=clock or (lambda: datetime.now(timezone.utc)), data_root=base.data_dir)
