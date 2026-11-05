"""Deleting an account (release step 6, task 4; spec §8).

`erase_user` removes everything LORE holds for one user:
- their library, `<root>/users/<id>/` (books, SQLite, Kuzu, LanceDB, caches);
- every central row: sessions, identities, the encrypted Gemini key, chats,
  messages, traces, reports, usage, jobs, in-flight slots, the rate counters
  keyed by them, their unspent sign-in links, and the user row itself;
- waitlist rows with their email are anonymised (address and note blanked).

It is idempotent and safe to resume. The first step marks the user
`deleting`, revokes every session and pauses their queued jobs, and is
committed on its own: from then on nobody can sign in as them, and a second
call (or the worker, see below) carries on from wherever the first stopped.
Rows go in one transaction; the directory goes before them, so a failure
leaves the `deleting` row behind to say the job is unfinished.

A book being processed at that moment (a claimed job with a fresh
heartbeat) holds the library open, so its directory is left for the worker:
after that job, `lore worker` sees its user gone or `deleting` and removes
the directory itself (`finish_pending`). Everything else is gone at once.

Admin events about the user are kept (they name only the id).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from lore.auth import ratelimit
from lore.central.models import (
    CLAIMED, QUEUED, AdminEvent, ApiKey, AuthSession, Chat, Identity, InflightSlot, Invite,
    Job, LoginToken, Message, RateHit, Report, Trace, Usage, User, Waitlist,
)
from lore.tenancy import validate_user_id

log = logging.getLogger("lore.erase")

DELETING = "deleting"
# Every rate counter keyed by a user id (lore.auth.ratelimit keys are hashed).
USER_RATE_KINDS = ("key_put", "upload", "upload_added", "ask", "intro", "report")
ANONYMISED = "anonymised@deleted.invalid"


@dataclass(frozen=True)
class Erased:
    user_id: str
    library_removed: bool  # False: a running job holds it; the worker removes it
    rows: dict[str, int]


def library_dir(root: Path, user_id: str) -> Path:
    """`<root>/users/<id>`, NOT created (lore.tenancy.user_data_dir makes it)."""
    validate_user_id(user_id)
    users = Path(root) / "users"
    path = users / user_id
    if path.exists() and path.resolve().parent != users.resolve():
        raise ValueError(f"user directory escapes the users root: {user_id!r}")
    return path


def remove_library(root: Path, user_id: str) -> bool:
    path = library_dir(root, user_id)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    return True


def _job_running(db: Session, user_id: str, now: datetime) -> bool:
    from lore.central.queue import STALE_AFTER
    return db.scalar(sa.select(sa.func.count()).select_from(Job).where(
        Job.user_id == user_id, Job.state == CLAIMED,
        Job.heartbeat_at > now - STALE_AFTER)) > 0


def lock_user(db: Session, user_id: str) -> str | None:
    """Mark the user `deleting`, revoke their sessions and pause their queued
    jobs. Commits. Returns their email (None: no such user any more)."""
    user = db.get(User, user_id)
    if user is None:
        return None
    user.status = DELETING
    db.execute(sa.delete(AuthSession).where(AuthSession.user_id == user_id))
    db.execute(sa.update(Job).where(Job.user_id == user_id, Job.state == QUEUED)
               .values(state="paused", outcome="deleting")
               .execution_options(synchronize_session=False))
    email = user.email
    db.commit()
    return email


def erase_user(sessions: sessionmaker[Session], root: Path, user_id: str, *,
               now: datetime) -> Erased:
    validate_user_id(user_id)
    with sessions() as db:
        email = lock_user(db, user_id)
        running = _job_running(db, user_id, now)
    removed = False
    if not running:
        removed = remove_library(root, user_id)
    counts: dict[str, int] = {}
    with sessions() as db:
        def gone(name: str, stmt) -> None:
            counts[name] = db.execute(stmt.execution_options(
                synchronize_session=False)).rowcount or 0

        chats = sa.select(Chat.id).where(Chat.user_id == user_id)
        gone("traces", sa.delete(Trace).where(Trace.user_id == user_id))
        gone("messages", sa.delete(Message).where(Message.chat_id.in_(chats)))
        gone("chats", sa.delete(Chat).where(Chat.user_id == user_id))
        gone("reports", sa.delete(Report).where(Report.user_id == user_id))
        gone("usage", sa.delete(Usage).where(Usage.user_id == user_id))
        gone("api_keys", sa.delete(ApiKey).where(ApiKey.user_id == user_id))
        gone("identities", sa.delete(Identity).where(Identity.user_id == user_id))
        gone("sessions", sa.delete(AuthSession).where(AuthSession.user_id == user_id))
        gone("jobs", sa.delete(Job).where(Job.user_id == user_id))
        gone("inflight_slots", sa.delete(InflightSlot).where(InflightSlot.user_id == user_id))
        gone("rate_hits", sa.delete(RateHit).where(RateHit.key.in_(
            [ratelimit._key(kind, user_id) for kind in USER_RATE_KINDS])))
        db.execute(sa.update(Invite).where(Invite.used_by == user_id).values(used_by=None))
        db.execute(sa.update(Invite).where(Invite.created_by == user_id).values(created_by=None))
        db.execute(sa.update(AdminEvent).where(AdminEvent.admin_id == user_id)
                   .values(admin_id=None))
        if email is not None:
            gone("login_tokens", sa.delete(LoginToken).where(LoginToken.email == email))
            gone("waitlist", sa.update(Waitlist).where(Waitlist.email == email)
                 .values(email=ANONYMISED, reading=None))
            db.execute(sa.update(Invite).where(Invite.email == email).values(email=None))
        gone("users", sa.delete(User).where(User.id == user_id))
        db.commit()
    log.info("account %s erased (library %s)", user_id,
             "removed" if removed else "left for the running job")
    return Erased(user_id, removed, counts)


def finish_pending(sessions: sessionmaker[Session], root: Path, user_id: str) -> bool:
    """After a job: if its user was deleted (or is being deleted) meanwhile,
    remove what is left. True when it did anything."""
    with sessions() as db:
        user = db.get(User, user_id)
        status = None if user is None else user.status
    if status is not None and status != DELETING:
        return False
    if status == DELETING:  # an erase that stopped half way: finish it all
        from datetime import timezone
        erase_user(sessions, root, user_id, now=datetime.now(timezone.utc))
        return True
    if library_dir(root, user_id).exists():
        remove_library(root, user_id)
        return True
    return False
