"""Weekly GPU-hour budget against Kaggle's free-tier allowance.

An unfinished run counts at its estimate; once the kernel reports real
GPU seconds the actual replaces it, so the ledger neither overbooks nor
strands quota.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

WEEKLY_LIMIT_HOURS = 30.0


class QuotaExceeded(RuntimeError):
    """Dispatching this run would exceed the weekly free-tier allowance."""


def iso_week(now: datetime | None = None) -> str:
    moment = now if now is not None else datetime.now(timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def hours_used(conn: sqlite3.Connection, week: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(gpu_hours_actual, gpu_hours_est)), 0.0)"
        " FROM quota_ledger WHERE week = ?",
        (week,),
    ).fetchone()
    return float(row[0])


def check_budget(
    conn: sqlite3.Connection,
    est_hours: float,
    week: str,
    limit_hours: float = WEEKLY_LIMIT_HOURS,
) -> None:
    used = hours_used(conn, week)
    if used + est_hours > limit_hours:
        raise QuotaExceeded(
            f"{week}: {used:.1f}h used + {est_hours:.1f}h estimated exceeds "
            f"the {limit_hours:.0f}h weekly allowance"
        )


def record_dispatch(
    conn: sqlite3.Connection, run_ref: str, est_hours: float, week: str
) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE quota_ledger SET gpu_hours_est = ?, gpu_hours_actual = NULL, week = ? WHERE run_ref = ?",
            (est_hours, week, run_ref),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO quota_ledger (run_ref, gpu_hours_est, week) VALUES (?, ?, ?)",
                (run_ref, est_hours, week),
            )


def record_actual(
    conn: sqlite3.Connection, run_ref: str, actual_hours: float
) -> None:
    with conn:
        cursor = conn.execute(
            "UPDATE quota_ledger SET gpu_hours_actual = ? WHERE run_ref = ?",
            (actual_hours, run_ref),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"record_actual({run_ref}): expected to update 1 row, but updated {cursor.rowcount}"
            )


def week_report(conn: sqlite3.Connection, week: str) -> dict:
    used = hours_used(conn, week)
    runs = conn.execute(
        "SELECT COUNT(*) FROM quota_ledger WHERE week = ?", (week,)
    ).fetchone()[0]
    return {
        "week": week,
        "used_hours": used,
        "remaining_hours": max(0.0, WEEKLY_LIMIT_HOURS - used),
        "runs": int(runs),
    }
