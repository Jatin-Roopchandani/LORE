"""The central database's engine and sessions.

`LORE_DATABASE_URL` picks the database: Postgres in production
(`postgresql+psycopg://user:pass@host/lore`), and by default a SQLite file
`central.db` beside the data root, which is what local mode and the tests use.
Engines are made by the caller and passed on; nothing here is a module-level
singleton, so a test (or a worker) owns the engine it makes.
"""
from __future__ import annotations

import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from lore.config import LoreConfig

ENV_URL = "LORE_DATABASE_URL"


def default_url(config: LoreConfig) -> str:
    """The SQLite file beside the data root (`<data_dir>/central.db`)."""
    return f"sqlite:///{Path(config.data_dir).resolve() / 'central.db'}"


def database_url(config: LoreConfig) -> str:
    return os.environ.get(ENV_URL) or default_url(config)


def make_engine(url: str) -> sa.Engine:
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        # Name the driver we install (psycopg 3), not SQLAlchemy's psycopg2 default.
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    engine = sa.create_engine(url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        _tune_sqlite(engine)
    return engine


def _tune_sqlite(engine: sa.Engine) -> None:
    """WAL and a busy timeout, and `BEGIN IMMEDIATE` for every transaction.

    A deferred transaction that reads and then writes can fail at once with
    "database is locked" when another connection wrote in between (no busy
    wait applies to that upgrade). Taking the write lock up front makes
    writers queue on the timeout instead, which is what the job claim needs.
    This is the pattern from SQLAlchemy's SQLite documentation.
    """

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _record):
        dbapi_conn.isolation_level = None  # we emit BEGIN ourselves
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def make_session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def upgrade(url: str, revision: str = "head") -> None:
    """Apply the Alembic migrations to the database at `url`."""
    from lore.central.migrate import upgrade as _upgrade
    _upgrade(url, revision)
