import sqlite3
from datetime import datetime, timezone

import pytest

from lore.db.sqlite import open_db
from lore.orchestrator.gemini_budget import (
    BudgetExceeded, calls_today, check_budget, day_report, record_call, today,
)


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "lore.db")
    yield connection
    connection.close()


def test_today_is_an_iso_date():
    assert today(datetime(2026, 9, 24, 13, 5, tzinfo=timezone.utc)) == "2026-09-24"


def test_unseen_model_has_no_calls(conn):
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 0


def test_record_call_accumulates(conn):
    for _ in range(3):
        record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 3


def test_models_and_days_are_counted_apart(conn):
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    record_call(conn, "gemini-3.1-flash-lite", "2026-09-24")
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-25")
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-24") == 1
    assert calls_today(conn, "gemini-3.1-flash-lite", "2026-09-24") == 1
    assert calls_today(conn, "gemini-3.5-flash-lite", "2026-09-25") == 1


def test_check_budget_passes_under_the_limit(conn):
    check_budget(conn, "gemini-3.5-flash-lite", "2026-09-24")


def test_check_budget_raises_at_the_limit(conn):
    from lore.inference.gemini import limits_for

    limit = limits_for("gemini-3.5-flash-lite").rpd
    conn.execute(
        "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, ?)",
        ("2026-09-24", "gemini-3.5-flash-lite", limit),
    )
    conn.commit()
    with pytest.raises(BudgetExceeded) as excinfo:
        check_budget(conn, "gemini-3.5-flash-lite", "2026-09-24")
    assert "gemini-3.5-flash-lite" in str(excinfo.value)
    assert str(limit) in str(excinfo.value)


def test_day_report_lists_each_model_with_remaining(conn):
    record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
    rows = day_report(conn, "2026-09-24")
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "gemini-3.5-flash-lite"
    assert row["count"] == 1
    assert row["remaining"] == row["limit"] - 1


# Review Focus 5: concurrent increments must not lose a count.
def test_concurrent_increments_do_not_lose_counts(tmp_path):
    path = tmp_path / "lore.db"
    open_db(path).close()  # migrate once, then open plain connections

    conns = [sqlite3.connect(path) for _ in range(4)]
    try:
        for _ in range(10):
            for c in conns:
                record_call(c, "gemini-3.5-flash-lite", "2026-09-24")
        checker = sqlite3.connect(path)
        try:
            assert calls_today(checker, "gemini-3.5-flash-lite", "2026-09-24") == 40
        finally:
            checker.close()
    finally:
        for c in conns:
            c.close()
