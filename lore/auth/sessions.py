"""Server-side sessions: a random token in a cookie, its hash in the database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.auth.tokens import hash_token, new_token
from lore.central.models import AuthSession, LoginToken, RateHit, User

LIFETIME = timedelta(days=30)
# The expiry slides on use, but is rewritten (and the cookie re-sent) at most
# this often, so a busy page does not write on every request.
REFRESH_AFTER = timedelta(days=1)
MAX_TOKEN_CHARS = 128  # a real token is 43; refuse to hash anything bigger


@dataclass(frozen=True)
class Resolved:
    user_id: str
    refreshed: bool  # the expiry moved: the cookie must be re-sent


def create_session(db: Session, user_id: str, now: datetime,
                   user_agent: str | None = None) -> str:
    """A new session for the user; returns the token to put in the cookie."""
    token = new_token()
    db.add(AuthSession(token_hash=hash_token(token), user_id=user_id,
                       created_at=now, refreshed_at=now, expires_at=now + LIFETIME,
                       user_agent=(user_agent or "")[:300] or None))
    purge_expired(db, now)
    db.flush()
    return token


def resolve(db: Session, token: str | None, now: datetime) -> Resolved | None:
    """The user the token belongs to, or None for anything not valid: unknown,
    expired, or a user who is not active. Slides the expiry (at most daily)."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    row = db.get(AuthSession, hash_token(token))
    if row is None or row.expires_at <= now:
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    refreshed = now - row.refreshed_at >= REFRESH_AFTER
    if refreshed:
        row.expires_at = now + LIFETIME
        row.refreshed_at = now
    return Resolved(user.id, refreshed)


def delete_session(db: Session, token: str | None) -> None:
    if token and len(token) <= MAX_TOKEN_CHARS:
        db.execute(sa.delete(AuthSession).where(
            AuthSession.token_hash == hash_token(token)))


def delete_user_sessions(db: Session, user_id: str) -> None:
    db.execute(sa.delete(AuthSession).where(AuthSession.user_id == user_id))


def purge_expired(db: Session, now: datetime) -> None:
    """Housekeeping on the sign-in path: expired sessions, used or expired
    login tokens, and rate counters older than a day."""
    db.execute(sa.delete(AuthSession).where(AuthSession.expires_at <= now))
    db.execute(sa.delete(LoginToken).where(
        sa.or_(LoginToken.expires_at <= now - timedelta(hours=1),
               LoginToken.used_at.is_not(None))))
    db.execute(sa.delete(RateHit).where(RateHit.at <= now - timedelta(days=1)))
