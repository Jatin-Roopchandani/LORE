"""A central database on a temp SQLite file, with users, for queue tests."""
from __future__ import annotations

from pathlib import Path

from lore.central import db as central
from lore.central.models import User


def central_sessions(tmp_path: Path, users=("alice", "bob")):
    """(sessionmaker, url): migrated, with a user row per id."""
    url = f"sqlite:///{tmp_path / 'central.db'}"
    central.upgrade(url)
    sessions = central.make_session_factory(central.make_engine(url))
    with sessions() as session:
        for user in users:
            session.add(User(id=user, email=f"{user}@example.org"))
        session.commit()
    return sessions, url
