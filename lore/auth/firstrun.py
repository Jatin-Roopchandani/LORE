"""First run in self-host mode: create the admin, exactly once.

Until an admin exists, `SetupGate` sends every page to /setup and answers
every API call 409 `setup_required`; only the setup screen, its API, the
static files and /healthz get through. POST /api/setup (lore.auth.selfhost)
calls `create_first_admin`, which succeeds once:

- the `server_settings` row `setup` is inserted in the same transaction as
  the admin, and its primary key lets only one transaction ever commit it,
  on SQLite and on Postgres alike;
- a server that already has an admin (`lore admin grant`) never offers setup.

The setup code (like Grafana's and Jupyter's): while no admin exists, a
random one-time code is kept in `<data root>/setup-code` (mode 600) and
printed in a box on every start, so only someone who can read the server's
log (`docker compose logs lore`) can create the admin, not whoever reaches
the port first. LORE_SETUP_CODE sets it instead, for unattended installs.
The file goes once the admin exists, and the code is never printed again.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import uuid
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lore.central.models import ServerSetting, User

SETUP_KEY = "setup"
SETUP_CODE_FILE = "setup-code"
ENV_SETUP_CODE = "LORE_SETUP_CODE"
# No 0/O, 1/I/L: read off a log and typed. 31 symbols, 12 of them: ~59 bits.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _normal(code: object) -> str:
    """Upper case, without spaces and dashes: how a code is compared."""
    if not isinstance(code, str):
        return ""
    return "".join(c for c in code.upper() if c not in " -\t")[:200]


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def current_setup_code(data_root: Path, environ=None) -> str:
    """The setup code: LORE_SETUP_CODE, else the one in the data root, made
    on first use (atomically: two processes starting together agree)."""
    env = os.environ if environ is None else environ
    given = (env.get(ENV_SETUP_CODE) or "").strip()
    if given:
        return given
    path = Path(data_root) / SETUP_CODE_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_new_code() + "\n")
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass  # another process made it first: use theirs
        finally:
            tmp.unlink(missing_ok=True)
    return path.read_text().strip()


def setup_code_matches(data_root: Path, given: object) -> bool:
    """Constant-time comparison, forgiving case, spaces and dashes."""
    expected = _normal(current_setup_code(data_root))
    return bool(expected) and hmac.compare_digest(expected.encode(), _normal(given).encode())


def forget_setup_code(data_root: Path) -> None:
    (Path(data_root) / SETUP_CODE_FILE).unlink(missing_ok=True)


def announce_setup_code(data_root: Path, origin: str, out=None) -> None:
    """The boxed log lines a new server prints on every start until setup."""
    code = current_setup_code(data_root)
    lines = [f"LORE setup code: {code}",
             f"Enter it at {origin.rstrip('/')}/setup to create the admin account."]
    width = max(len(line) for line in lines)
    box = ["+" + "-" * (width + 2) + "+", *(f"| {line.ljust(width)} |" for line in lines),
           "+" + "-" * (width + 2) + "+"]
    print("\n".join(box), file=out or sys.stderr, flush=True)


def prepare_setup(sessions: sessionmaker, data_root: Path, origin: str) -> None:
    """On start: print the code while setup is open; else make sure no code
    file is left behind."""
    with sessions() as db:
        needed = setup_needed(db)
    if needed:
        announce_setup_code(data_root, origin)
    else:
        forget_setup_code(data_root)


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
