"""First run in self-host mode: create the admin, exactly once.

Until an admin exists, `SetupGate` sends every page to /setup and answers
every API call 409 `setup_required`; only the setup screen, its API, the
static files and /healthz get through. POST /api/setup (lore.auth.selfhost)
calls `create_first_admin`, which succeeds once:

- the `server_settings` row `setup` is inserted in the same transaction as
  the admin, and its primary key lets only one transaction ever commit it,
  on SQLite and on Postgres alike;
- a server that already has an admin (`lore admin grant`) never offers setup.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lore.central.models import ServerSetting, User

SETUP_KEY = "setup"


class SetupDone(Exception):
    """The admin was already created: setup is closed."""


def setup_needed(db: Session) -> bool:
    if db.get(ServerSetting, SETUP_KEY) is not None:
        return False
    admins = db.scalar(sa.select(sa.func.count()).select_from(User)
                       .where(User.role == "admin"))
    return not admins


def create_first_admin(sessions: sessionmaker, email: str, password_hash: str,
                       now: datetime, name: str | None = None) -> str:
    """The new admin's id, or SetupDone when setup is already closed (another
    request won the race, or an admin exists). `email` is normalized and the
    password hashed by the caller (hashing is slow: never under the lock)."""
    with sessions() as db:
        try:
            if not setup_needed(db):
                raise SetupDone()
            db.add(ServerSetting(key=SETUP_KEY, value={"at": now.isoformat()},
                                 updated_at=now))
            db.flush()  # a racing transaction that inserted it first fails here
            user = db.scalars(sa.select(User).where(User.email == email)).first()
            if user is None:
                user = User(id=str(uuid.uuid4()), email=email, created_at=now,
                            status="active")
                db.add(user)
            user.role, user.status = "admin", "active"
            user.password_hash = password_hash
            if name:
                user.name = name[:200]
            db.flush()
            db.execute(sa.update(ServerSetting).where(ServerSetting.key == SETUP_KEY)
                       .values(updated_by=user.id))
            db.commit()
            return user.id
        except IntegrityError:
            db.rollback()
            raise SetupDone() from None


# --- the gate ----------------------------------------------------------------

# Reachable before the admin exists. Everything else waits for setup.
_OPEN_PATHS = frozenset({"/setup", "/api/setup", "/healthz"})
_API_PREFIXES = ("/api/", "/auth/", "/mcp", "/oauth/", "/.well-known/", "/token",
                 "/register", "/authorize", "/revoke")


def _static(path: str) -> bool:
    """A file of the built web app (its scripts, styles, fonts, icons)."""
    last = path.rsplit("/", 1)[-1]
    return path.startswith("/assets/") or ("." in last and not path.startswith("/api/"))


class SetupGate:
    """Pure ASGI middleware: while setup is needed, pages redirect to /setup
    and API calls answer 409 `setup_required`. Once an admin exists that is
    remembered for the life of the process, so a set-up server pays one
    query per process, not per request."""

    def __init__(self, app, sessions) -> None:
        self.app = app
        self._sessions = sessions  # a callable returning the sessionmaker
        self._done = False

    def done(self) -> bool:
        if not self._done:
            with self._sessions()() as db:
                self._done = not setup_needed(db)
        return self._done

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if path in _OPEN_PATHS or _static(path) or self.done():
            return await self.app(scope, receive, send)
        if path.startswith(_API_PREFIXES) or path == "/api":
            body = json.dumps({"error": {
                "code": "setup_required",
                "message": "Create the admin account first: open /setup."}}).encode()
            await send({"type": "http.response.start", "status": 409, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            return
        await send({"type": "http.response.start", "status": 303, "headers": [
            (b"location", b"/setup"), (b"cache-control", b"no-store"),
            (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})
