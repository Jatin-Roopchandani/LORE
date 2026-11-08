"""A small rate limit on the central database: N hits per key per window.

A counter per key (email, IP) kept as rows in `rate_hits`, so it is shared by
every web process and survives a restart. Keys are hashed, so the table holds
no addresses. Over the limit, the request is refused and NOT recorded, so the
window slides rather than extending a lockout.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from lore.central.models import RateHit

WINDOW = timedelta(hours=1)


def _key(kind: str, who: str) -> str:
    return f"{kind}:{hashlib.sha256(who.encode()).hexdigest()[:48]}"


def allow(db: Session, kind: str, who: str, limit: int, now: datetime,
          window: timedelta = WINDOW) -> bool:
    """Record a hit and return True, or return False if `who` is at the limit."""
    key = _key(kind, who)
    seen = db.scalar(sa.select(sa.func.count()).select_from(RateHit).where(
        RateHit.key == key, RateHit.at > now - window))
    if seen >= limit:
        return False
    db.add(RateHit(key=key, at=now))
    db.flush()
    return True
