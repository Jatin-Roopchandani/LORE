"""Signed-in devices: the user's own live sessions, listed and revoked.

A session row is keyed by the SHA-256 of its cookie. The page names a session
by the first 16 hex characters of that hash (`public_id`): enough to tell a
user's few sessions apart, and nothing that can be presented as a cookie (the
hash cannot be turned back into the token, and even the full hash signs no one
in). Every query is filtered by the user's id, so one user can neither see
nor end another's session, whatever id they send.
"""
from __future__ import annotations

import re
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.auth.sessions import MAX_TOKEN_CHARS
from lore.auth.tokens import hash_token
from lore.central.models import AuthSession

ID_CHARS = 16
_ID = re.compile(r"^[0-9a-f]{16}$")

# Most specific first: Edge and Opera say "Chrome" too, and Chrome says "Safari".
_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
             ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"), ("Chrome/", "Chrome"),
             ("Safari/", "Safari"))
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
            ("CrOS", "ChromeOS"), ("Mac OS X", "macOS"), ("Macintosh", "macOS"),
            ("Windows", "Windows"), ("Linux", "Linux"))


def public_id(token_hash: str) -> str:
    return token_hash[:ID_CHARS]


def valid_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def device_label(user_agent: str | None) -> str:
    """"Firefox on Linux", from the User-Agent; "Unknown browser" without one."""
    ua = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in ua), None)
    system = next((name for mark, name in _SYSTEMS if mark in ua), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown browser"


def current_hash(token: str | None) -> str | None:
    return hash_token(token) if token and len(token) <= MAX_TOKEN_CHARS else None


def list_sessions(db: Session, user_id: str, now: datetime,
                  current: str | None) -> list[dict]:
    """The user's live sessions, most recently used first; `current` is the
    hash of this request's session, marked as "this device"."""
    rows = db.scalars(sa.select(AuthSession).where(
        AuthSession.user_id == user_id, AuthSession.expires_at > now)
        .order_by(AuthSession.refreshed_at.desc())).all()
    return [{"id": public_id(r.token_hash), "device": device_label(r.user_agent),
             "created_at": r.created_at.isoformat(),
             "last_active": r.refreshed_at.isoformat(),
             "current": r.token_hash == current}
            for r in rows]


def revoke(db: Session, user_id: str, sid: str) -> int:
    """End the user's session named `sid`; how many rows went (0 or 1)."""
    if not valid_id(sid):
        return 0
    rows = db.scalars(sa.select(AuthSession).where(
        AuthSession.user_id == user_id,
        AuthSession.token_hash.like(sid + "%"))).all()
    for row in rows:
        db.delete(row)
    return len(rows)


def revoke_others(db: Session, user_id: str, keep: str | None) -> int:
    """End every session of the user's but `keep` (a hash); how many went."""
    query = sa.delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep:
        query = query.where(AuthSession.token_hash != keep)
    return db.execute(query).rowcount or 0
