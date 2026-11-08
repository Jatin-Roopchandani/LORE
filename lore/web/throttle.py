"""Rate limits and concurrency caps (release step 6, task 2). Multi mode only.

Two kinds of limit, both kept in the central database so every web process
shares them (the same style as lore.auth.ratelimit):

- **Rates**: N per user per window, counted as rows in `rate_hits`
  (`lore.auth.ratelimit`). A refused request is not counted, so the window
  slides; the refusal says how long until the oldest counted hit leaves it.
- **In flight**: at most N at once, as N possible rows of `inflight_slots`
  (`kind`, `slot` 0..N-1, the primary key). Taking a slot is an INSERT that
  either succeeds or hits the key, which is atomic on SQLite and Postgres
  and across processes. Each slot is a lease (`expires_at`), so a process
  that died mid-question frees its slots when the lease runs out.

Past a limit the answer is 429 with `retry_after` (seconds, also the
Retry-After header) and a friendly message.

| Limit | Default | Variable |
|---|---|---|
| questions per user per minute | 6 | LORE_ASK_PER_MINUTE |
| questions in flight per user | 1 | LORE_ASK_IN_FLIGHT_PER_USER |
| questions in flight, all users | 8 | LORE_ASK_IN_FLIGHT |
| entity intro model calls per user per hour | 60 | LORE_INTRO_PER_HOUR |
| answer reports per user per hour | 20 | LORE_REPORTS_PER_HOUR |
| uploads being parsed at once, all users | 2 | LORE_PARSES_IN_FLIGHT |
| MCP tool calls per token per minute | 30 | LORE_MCP_PER_MINUTE |
| MCP tool calls per user per minute (all their tokens) | 60 | LORE_MCP_PER_MINUTE_PER_USER |

An MCP `lore_search` also takes the user's question slots and counts against
their questions a minute, exactly as a question asked in the web app does.

0 switches that limit off. A value that is not a whole number, or is
negative, stops start-up.
"""
from __future__ import annotations

import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from lore.auth import ratelimit
from lore.central.models import InflightSlot, RateHit

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
ASK_LEASE = timedelta(minutes=15)    # longer than any answer takes
PARSE_LEASE = timedelta(minutes=10)  # the parse's own wall-clock cap is shorter
BUSY_RETRY = 5     # seconds: one's own question is still running
GLOBAL_RETRY = 15  # seconds: everyone's questions or uploads are

ENV = {
    "ask_per_minute": "LORE_ASK_PER_MINUTE",
    "ask_in_flight_per_user": "LORE_ASK_IN_FLIGHT_PER_USER",
    "ask_in_flight": "LORE_ASK_IN_FLIGHT",
    "intro_per_hour": "LORE_INTRO_PER_HOUR",
    "reports_per_hour": "LORE_REPORTS_PER_HOUR",
    "parses_in_flight": "LORE_PARSES_IN_FLIGHT",
    "mcp_per_minute": "LORE_MCP_PER_MINUTE",
    "mcp_per_minute_per_user": "LORE_MCP_PER_MINUTE_PER_USER",
}

ASK_RATE, INTRO_RATE, REPORT_RATE = "ask", "intro", "report"
# MCP calls: per token (keyed by the token's id, never the token) and per user.
MCP_RATE, MCP_USER_RATE = "mcp", "mcp_user"
# The rate_hits kinds keyed by user id (lore.central.erase deletes them).
USER_RATE_KINDS = (ASK_RATE, INTRO_RATE, REPORT_RATE, MCP_USER_RATE)


def _env_int(env, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class RateSettings:
    ask_per_minute: int = 6
    ask_in_flight_per_user: int = 1
    ask_in_flight: int = 8
    intro_per_hour: int = 60
    reports_per_hour: int = 20
    parses_in_flight: int = 2
    mcp_per_minute: int = 30
    mcp_per_minute_per_user: int = 60

    @classmethod
    def from_env(cls, env=None) -> "RateSettings":
        env = os.environ if env is None else env
        defaults = cls()
        return cls(**{field: _env_int(env, name, getattr(defaults, field))
                      for field, name in ENV.items()})


def too_many(code: str, message: str, retry_after: float) -> JSONResponse:
    seconds = max(1, math.ceil(retry_after))
    return JSONResponse(
        {"error": {"code": code, "message": message, "retry_after": seconds},
         "retry_after": seconds},
        status_code=429, headers={"Retry-After": str(seconds), "Cache-Control": "no-store"})


# --- rates ----------------------------------------------------------------------

def wait_for(db: Session, kind: str, who: str, limit: int, now: datetime,
             window: timedelta) -> float | None:
    """Seconds until `who` may make one more `kind` request, or None if now."""
    if limit <= 0:
        return None
    key = ratelimit._key(kind, who)
    hits = list(db.scalars(sa.select(RateHit.at).where(
        RateHit.key == key, RateHit.at > now - window).order_by(RateHit.at)))
    if len(hits) < limit:
        return None
    frees = hits[len(hits) - limit] + window  # when the count drops below the limit
    return max(1.0, (frees - now).total_seconds())


def record(db: Session, kind: str, who: str, now: datetime, n: int = 1) -> None:
    key = ratelimit._key(kind, who)
    for _ in range(n):
        db.add(RateHit(key=key, at=now))
    db.flush()


def take(db: Session, kind: str, who: str, limit: int, now: datetime,
         window: timedelta) -> float | None:
    """Count one `kind` request for `who` and return None, or return the
    seconds to wait (and count nothing)."""
    wait = wait_for(db, kind, who, limit, now, window)
    if wait is None and limit > 0:
        record(db, kind, who, now)
    return wait


# --- in flight -------------------------------------------------------------------

class Slots:
    """N-at-once leases on the central database."""

    def __init__(self, sessions: Callable[[], sessionmaker],
                 now: Callable[[], datetime]) -> None:
        self._sessions, self._now = sessions, now

    def acquire(self, kind: str, limit: int, *, user_id: str | None = None,
                lease: timedelta = ASK_LEASE) -> str | None:
        """A holder token for one of `kind`'s `limit` slots, or None when all
        are taken. limit 0: unlimited (a token, nothing stored)."""
        if limit <= 0:
            return ""
        now = self._now()
        holder = uuid.uuid4().hex
        with self._sessions()() as db:
            db.execute(sa.delete(InflightSlot).where(
                InflightSlot.kind == kind, InflightSlot.expires_at <= now))
            db.commit()
        for slot in range(limit):
            with self._sessions()() as db:
                db.add(InflightSlot(kind=kind, slot=slot, holder=holder,
                                    user_id=user_id, expires_at=now + lease))
                try:
                    db.commit()
                    return holder
                except IntegrityError:  # taken: try the next one
                    db.rollback()
        return None

    def release(self, kind: str, holder: str | None) -> None:
        if not holder:
            return
        with self._sessions()() as db:
            db.execute(sa.delete(InflightSlot).where(
                InflightSlot.kind == kind, InflightSlot.holder == holder))
            db.commit()


@dataclass
class Grant:
    """The slots one question holds; `release` gives them back (idempotent)."""
    slots: Slots
    held: list[tuple[str, str]]

    def release(self) -> None:
        held, self.held = self.held, []
        for kind, holder in held:
            self.slots.release(kind, holder)


class Throttle:
    def __init__(self, sessions: Callable[[], sessionmaker], settings: RateSettings,
                 now: Callable[[], datetime]) -> None:
        self.settings, self._sessions, self._now = settings, sessions, now
        self.slots = Slots(sessions, now)

    def start_question(self, user_id: str) -> Grant | JSONResponse:
        """The user's question may start (a Grant to release when it ends), or
        the 429 to send instead."""
        s = self.settings
        user_kind = f"ask:user:{user_id}"
        mine = self.slots.acquire(user_kind, s.ask_in_flight_per_user, user_id=user_id)
        if mine is None:
            return too_many("busy", "LORE is still answering your previous question.",
                            BUSY_RETRY)
        grant = Grant(self.slots, [(user_kind, mine)])
        everyone = self.slots.acquire("ask:all", s.ask_in_flight, user_id=user_id)
        if everyone is None:
            grant.release()
            return too_many("busy", "LORE is answering a lot of questions right now."
                            " Try again in a moment.", GLOBAL_RETRY)
        grant.held.append(("ask:all", everyone))
        with self._sessions()() as db:
            wait = take(db, ASK_RATE, user_id, s.ask_per_minute, self._now(), MINUTE)
            db.commit()
        if wait is not None:
            grant.release()
            return too_many("rate_limited",
                            f"You can ask {s.ask_per_minute} questions a minute."
                            " Please wait a moment.", wait)
        return grant

    def intro_wait(self, user_id: str) -> JSONResponse | None:
        s = self.settings
        with self._sessions()() as db:
            wait = wait_for(db, INTRO_RATE, user_id, s.intro_per_hour, self._now(), HOUR)
        if wait is None:
            return None
        return too_many("rate_limited", "Too many introductions this hour."
                        " Please try again later.", wait)

    def intro_spent(self, user_id: str, requests: int) -> None:
        if requests <= 0 or self.settings.intro_per_hour <= 0:
            return
        with self._sessions()() as db:
            record(db, INTRO_RATE, user_id, self._now(), requests)
            db.commit()

    def report(self, db: Session, user_id: str) -> JSONResponse | None:
        """Counts a report in `db` (the caller commits), or the 429."""
        wait = take(db, REPORT_RATE, user_id, self.settings.reports_per_hour,
                    self._now(), HOUR)
        if wait is None:
            return None
        return too_many("rate_limited", "Too many reports this hour."
                        " Please try again later.", wait)

    def start_parse(self) -> str | JSONResponse:
        holder = self.slots.acquire("parse:all", self.settings.parses_in_flight,
                                    lease=PARSE_LEASE)
        if holder is None:
            return too_many("busy", "LORE is reading other books right now."
                            " Try again in a moment.", GLOBAL_RETRY)
        return holder

    def mcp_wait(self, token_key: str, user_id: str) -> float | None:
        """Count one MCP tool call for the token and its user and return None,
        or return the seconds to wait (and count nothing)."""
        s, now = self.settings, self._now()
        with self._sessions()() as db:
            waits = [w for w in (
                wait_for(db, MCP_RATE, token_key, s.mcp_per_minute, now, MINUTE),
                wait_for(db, MCP_USER_RATE, user_id, s.mcp_per_minute_per_user, now, MINUTE),
            ) if w is not None]
            if waits:
                return max(waits)
            if s.mcp_per_minute > 0:
                record(db, MCP_RATE, token_key, now)
            if s.mcp_per_minute_per_user > 0:
                record(db, MCP_USER_RATE, user_id, now)
            db.commit()
        return None

    def end_parse(self, holder: str) -> None:
        self.slots.release("parse:all", holder)
