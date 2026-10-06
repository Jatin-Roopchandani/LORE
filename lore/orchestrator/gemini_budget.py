"""Daily Gemini request budget, shared by every caller.

Sibling to quota.py, which does the same job for Kaggle GPU-hours. The
point of one ledger is that `lore ask` in a terminal and `lore_search`
over MCP spend the SAME daily allowance: a server-only counter reports a
number that is wrong on any day the CLI was also used, which is most days.

The ledger day is Google's day: it rolls over at midnight Pacific
(America/Los_Angeles, so PST or PDT as the date demands), when Google's
free-tier allowance resets. That is what makes the refusal message's
"resets at midnight Pacific" true of LORE's own refusal. Do not "simplify"
this back to `datetime.now()`: this machine's local zone is UTC, and a UTC
day rolls over at 16:00/17:00 Pacific, so every request between 00:00 and
~07:00 UTC landed on a fresh ledger day while Google was still charging
the old one -- the ledger approved searches Google then refused with 429.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from lore.db.sqlite import open_db
from lore.inference.gemini import limits_for


class BudgetExceeded(RuntimeError):
    """This call would exceed the model's free-tier daily allowance."""


# Resolved from the system zone database (/usr/share/zoneinfo); there is no
# `tzdata` wheel in this environment.
GOOGLE_DAY_ZONE = ZoneInfo("America/Los_Angeles")


def today(now: datetime | None = None) -> str:
    """The ledger day (YYYY-MM-DD) in Google's free-tier zone, Pacific.

    `now` is for tests and must be timezone-aware; it is converted to
    Pacific first. A naive datetime is refused rather than guessed at:
    Python would read it as the machine's local zone (UTC here), and that
    guess is exactly the drift this function exists to remove.
    """
    if now is None:
        moment = datetime.now(GOOGLE_DAY_ZONE)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("today() needs a timezone-aware datetime, not a"
                         " naive one: its zone cannot be guessed")
    else:
        moment = now.astimezone(GOOGLE_DAY_ZONE)
    return moment.strftime("%Y-%m-%d")


def record_call(conn: sqlite3.Connection, model: str, day: str) -> None:
    """Count one request against (day, model).

    An upsert, not a read-then-write: two tool calls in flight would
    otherwise both read N and both write N+1, losing a request from the
    count. The UNIQUE index on (day, model) is what makes it atomic.
    """
    with conn:
        conn.execute(
            "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, 1)"
            " ON CONFLICT(day, model) DO UPDATE SET count = count + 1",
            (day, model),
        )


def calls_today(conn: sqlite3.Connection, model: str, day: str) -> int:
    row = conn.execute(
        "SELECT count FROM gemini_ledger WHERE day = ? AND model = ?",
        (day, model),
    ).fetchone()
    return int(row[0]) if row else 0


def check_budget(
    conn: sqlite3.Connection, model: str, day: str, headroom: int = 1
) -> None:
    used = calls_today(conn, model, day)
    limit = limits_for(model).rpd
    if used + headroom > limit:
        raise BudgetExceeded(
            f"{model}: {used} of {limit} free-tier requests used on {day}."
            " The allowance resets at midnight Pacific."
        )


def recorder_for(sqlite_path: Path) -> Callable[[str], None]:
    """A recorder that writes each attempt to the ledger at `sqlite_path`.

    Opens a connection per call, through `open_db` rather than a raw
    `sqlite3.connect`: this is the one place every recorder write passes
    through, so it is where the schema being current has to be guaranteed
    structurally, not by every call site remembering to run `open_db`
    itself first. `open_db` is idempotent (`IF NOT EXISTS`, and each
    migration only runs once, tracked in `schema_migrations`), so a
    database already at the current version pays only the cost of reading
    that it is -- a fraction of a millisecond against an HTTP round-trip to
    Google. Opening per call also keeps the recorder safe to hand to a
    client that outlives any one caller's connection -- a cached handle
    here would be a use-after-close waiting to happen.
    """
    def record(model: str) -> None:
        conn = open_db(sqlite_path)
        try:
            record_call(conn, model, today())
        finally:
            conn.close()
    return record


def day_report(conn: sqlite3.Connection, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT model, count FROM gemini_ledger WHERE day = ? ORDER BY model",
        (day,),
    ).fetchall()
    report = []
    for model, count in rows:
        limit = limits_for(model).rpd
        report.append({
            "model": model,
            "count": int(count),
            "limit": limit,
            "remaining": max(0, limit - int(count)),
        })
    return report
