"""Users, identities and invites: who may sign in, and who is a new account.

The rules, in order, for a sign-in that has PROVEN an email (an OAuth provider
that says it is verified, or a magic link that was opened):

1. The identity (provider, provider id) is known: that user, if active.
2. A user with that email exists: link the identity to that user, so the same
   verified email across providers is one account. If active.
3. Otherwise a NEW user. While the beta is invite-only that needs a valid
   unused invite, which this sign-in then consumes. Without one the person
   goes to the waitlist and nothing is created.

Existing users never need an invite.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.auth.tokens import hash_token, new_token
from lore.central.models import Identity, Invite, User

# One @, no whitespace or controls, a dot in the domain; deliberately loose
# (the real check is that a link or a provider proved the address).
_EMAIL = re.compile(r"[^@\s<>\"',;]{1,64}@[^@\s<>\"',;]{1,255}\.[^@\s<>\"',;]{2,63}")
_INVITE = re.compile(r"[A-Za-z0-9_-]{16,128}")
INVITE_LIFETIME = timedelta(days=14)


class Outcome(Enum):
    OK = "ok"
    NEEDS_INVITE = "needs_invite"
    DISABLED = "disabled"


@dataclass(frozen=True)
class SignIn:
    outcome: Outcome
    user_id: str | None = None


def normalize_email(email: object) -> str | None:
    """The address, trimmed and lowercased, or None if it does not look like one."""
    if not isinstance(email, str):
        return None
    value = email.strip().lower()
    if len(value) > 320 or not _EMAIL.fullmatch(value):
        return None
    return value


def clean_invite(code: object) -> str | None:
    """The invite code if it has the shape of one we issue, else None."""
    return code if isinstance(code, str) and _INVITE.fullmatch(code) else None


def create_invite(db: Session, created_by: str | None, now: datetime,
                  lifetime: timedelta = INVITE_LIFETIME) -> str:
    """A new single-use invite; returns the code, which is not stored."""
    code = new_token()
    db.add(Invite(code_hash=hash_token(code), created_by=created_by,
                  created_at=now, expires_at=now + lifetime))
    db.flush()
    return code


def invite_usable(db: Session, invite_hash: str | None, now: datetime) -> bool:
    if not invite_hash:
        return False
    row = db.get(Invite, invite_hash)
    return (row is not None and row.used_by is None
            and (row.expires_at is None or row.expires_at > now))


def _claim_invite(db: Session, invite_hash: str, user_id: str, now: datetime) -> bool:
    """Atomic: of two sign-ins racing for one invite, exactly one wins."""
    result = db.execute(
        sa.update(Invite)
        .where(Invite.code_hash == invite_hash, Invite.used_by.is_(None),
               sa.or_(Invite.expires_at.is_(None), Invite.expires_at > now))
        .values(used_by=user_id))
    return result.rowcount == 1


def user_by_email(db: Session, email: str) -> User | None:
    return db.scalars(sa.select(User).where(User.email == email)).first()


def can_sign_in(db: Session, email: str, invite_hash: str | None, now: datetime,
                invite_only: bool) -> bool:
    """Would a link to this address lead anywhere? (Decides whether to mail.)"""
    user = user_by_email(db, email)
    if user is not None:
        return user.status == "active"
    return (not invite_only) or invite_usable(db, invite_hash, now)


def sign_in_with_identity(db: Session, provider: str, subject: str, email: str,
                          name: str | None, invite_hash: str | None,
                          invite_only: bool, now: datetime) -> SignIn:
    """Find or create the user for a proven (provider, subject, email).

    The caller commits on OK and rolls back otherwise. `email` must already be
    normalized and verified.
    """
    identity = db.scalars(sa.select(Identity).where(
        Identity.provider == provider, Identity.provider_user_id == subject)).first()
    if identity is not None:
        user = db.get(User, identity.user_id)
        return _active(user)

    user = user_by_email(db, email)
    if user is not None:
        if user.status != "active":
            return SignIn(Outcome.DISABLED)
        db.add(Identity(provider=provider, provider_user_id=subject,
                        user_id=user.id, created_at=now))
        if not user.name and name:
            user.name = name[:200]
        db.flush()
        return SignIn(Outcome.OK, user.id)

    user = User(id=str(uuid.uuid4()), email=email, name=(name or None) and name[:200],
                role="user", status="active", created_at=now)
    db.add(user)
    db.flush()  # the invite's used_by points at this row
    if invite_only and not (invite_hash and _claim_invite(db, invite_hash, user.id, now)):
        db.delete(user)
        db.flush()
        return SignIn(Outcome.NEEDS_INVITE)
    db.add(Identity(provider=provider, provider_user_id=subject, user_id=user.id,
                    created_at=now))
    db.flush()
    return SignIn(Outcome.OK, user.id)


def _active(user: User | None) -> SignIn:
    if user is None or user.status != "active":
        return SignIn(Outcome.DISABLED)
    return SignIn(Outcome.OK, user.id)


def grant_admin(db: Session, email: str) -> tuple[User, bool]:
    """Make the user with this (normalized) email an admin, creating the
    account if there is none. Returns (user, created)."""
    from lore.central.models import utcnow
    user = user_by_email(db, email)
    created = user is None
    if user is None:
        user = User(id=str(uuid.uuid4()), email=email, role="admin",
                    status="active", created_at=utcnow())
        db.add(user)
    user.role = "admin"
    db.flush()
    return user, created
