"""Personal access tokens for the hosted MCP endpoint (multi mode).

A token is `lore_` plus 256 random bits (`secrets.token_urlsafe(32)`), shown to
its owner once, when it is made. Only its SHA-256 is stored (the same rule as
session ids and magic links, lore.auth.tokens), with a short prefix so a list
can tell tokens apart. A database copy holds nothing that works as a token.

A token reads its owner's library through /mcp and nothing else: the web API
authenticates by session cookie only, so a token can neither mint tokens nor
reach any /api route. Revoking marks the row (`revoked_at`); a revoked token,
an unknown one and one whose owner is not active all fail the same way.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.auth.tokens import hash_token
from lore.central.models import McpToken, User

TOKEN_PREFIX = "lore_"
PREFIX_CHARS = 12          # `lore_` and the first 7 random characters, for display
MAX_TOKEN_CHARS = 128      # a real token is 48; refuse to hash anything bigger
MAX_TOKENS_PER_USER = 10   # active (unrevoked) tokens
MAX_NAME_CHARS = 100
CREATES_PER_HOUR = 20      # per user, every attempt counted (lore.web.mcp_tokens)
# last_used_at is written at most this often, so a busy client does not
# write on every call.
LAST_USED_EVERY = timedelta(minutes=1)


class TooManyTokens(Exception):
    """The user already has MAX_TOKENS_PER_USER active tokens."""


class BadName(ValueError):
    """A token name must be 1-100 printable characters."""


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def clean_name(name: object) -> str:
    if not isinstance(name, str):
        raise BadName("a token needs a name")
    name = name.strip()
    if not name or len(name) > MAX_NAME_CHARS or not name.isprintable():
        raise BadName(f"a token name is 1-{MAX_NAME_CHARS} printable characters")
    return name


def _active(db: Session, user_id: str) -> int:
    return db.scalar(sa.select(sa.func.count()).select_from(McpToken).where(
        McpToken.user_id == user_id, McpToken.revoked_at.is_(None))) or 0


def create(db: Session, user_id: str, name: object, now: datetime) -> tuple[McpToken, str]:
    """A new token for `user_id`: (its row, the token itself). The caller
    commits and shows the token once; it is never stored or logged."""
    name = clean_name(name)
    if _active(db, user_id) >= MAX_TOKENS_PER_USER:
        raise TooManyTokens()
    raw = new_token()
    row = McpToken(id=uuid.uuid4().hex, user_id=user_id, name=name,
                   token_hash=hash_token(raw), prefix=raw[:PREFIX_CHARS],
                   created_at=now)
    db.add(row)
    db.flush()
    return row, raw


def public(row: McpToken) -> dict:
    """What a list shows of a token: never the token, never its hash."""
    return {"id": row.id, "name": row.name, "prefix": row.prefix,
            "created_at": row.created_at.isoformat(),
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None}


def list_for(db: Session, user_id: str) -> list[dict]:
    rows = db.scalars(sa.select(McpToken).where(
        McpToken.user_id == user_id, McpToken.revoked_at.is_(None))
        .order_by(McpToken.created_at, McpToken.id)).all()
    return [public(r) for r in rows]


def revoke(db: Session, user_id: str, token_id: str, now: datetime) -> bool:
    """Revoke one of `user_id`'s own tokens. False for an unknown token, one
    already revoked, or someone else's (all look the same)."""
    done = db.execute(sa.update(McpToken).where(
        McpToken.id == token_id, McpToken.user_id == user_id,
        McpToken.revoked_at.is_(None)).values(revoked_at=now)
        .execution_options(synchronize_session=False))
    return (done.rowcount or 0) == 1


def looks_like(raw: object) -> bool:
    return (isinstance(raw, str) and raw.startswith(TOKEN_PREFIX)
            and len(TOKEN_PREFIX) < len(raw) <= MAX_TOKEN_CHARS)


def resolve(db: Session, raw: object, now: datetime) -> McpToken | None:
    """The token's row when it is live and its owner active, else None.
    Stamps `last_used_at` (at most once a LAST_USED_EVERY); the caller commits."""
    if not looks_like(raw):
        return None
    row = db.scalar(sa.select(McpToken).where(McpToken.token_hash == hash_token(raw)))
    if row is None or row.revoked_at is not None:
        return None
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        return None
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_EVERY:
        row.last_used_at = now
    return row
