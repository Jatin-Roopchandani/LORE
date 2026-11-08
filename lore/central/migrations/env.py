"""Alembic environment: the URL comes from `lore.central.migrate`."""
from __future__ import annotations

from alembic import context

from lore.central.db import make_engine
from lore.central.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = make_engine(config.attributes["url"])
    try:
        with engine.connect() as connection:
            context.configure(connection=connection,
                              target_metadata=target_metadata,
                              render_as_batch=connection.dialect.name == "sqlite")
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
