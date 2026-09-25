"""Daily Gemini request budget, shared by every caller.

Sibling to quota.py, which does the same job for Kaggle GPU-hours. The
point of one ledger is that `lore ask` in a terminal and `lore_search`
over MCP spend the SAME daily allowance: a server-only counter reports a
number that is wrong on any day the CLI was also used, which is most days.

The day is LOCAL, not UTC. Google's free-tier day resets at midnight
Pacific, which this does not model; a local day is the honest
approximation and the report says which day it counted.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from lore.inference.gemini import limits_for


class BudgetExceeded(RuntimeError):
    """This call would exceed the model's free-tier daily allowance."""


def today(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now()
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
