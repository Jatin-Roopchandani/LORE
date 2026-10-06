import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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


# Google's free-tier day resets at midnight Pacific. A UTC instant before
# ~07:00 is still the PREVIOUS Pacific day: the ledger must charge it there,
# or it approves a search Google then refuses with a 429.
@pytest.mark.parametrize("utc,day", [
    (datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc), "2026-09-23"),
    (datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc), "2026-09-24"),
    # Winter (PST, UTC-8): 07:30 UTC is still 23:30 the day before.
    (datetime(2026, 1, 15, 7, 30, tzinfo=timezone.utc), "2026-01-14"),
    (datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc), "2026-01-15"),
])
def test_today_follows_the_pacific_day(utc, day):
    assert today(utc) == day


def test_today_without_an_argument_is_the_pacific_day():
    from zoneinfo import ZoneInfo
    expected = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert today() == expected


def test_today_refuses_a_naive_datetime():
    # A naive moment has no zone to convert from; guessing one is exactly
    # how the ledger day drifted from Google's.
    with pytest.raises(ValueError, match="timezone"):
        today(datetime(2026, 9, 24, 5, 0))


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


def test_check_budget_passes_at_limit_minus_one(conn):
    from lore.inference.gemini import limits_for

    limit = limits_for("gemini-3.5-flash-lite").rpd
    conn.execute(
        "INSERT INTO gemini_ledger (day, model, count) VALUES (?, ?, ?)",
        ("2026-09-24", "gemini-3.5-flash-lite", limit - 1),
    )
    conn.commit()
    # Should not raise: we're at limit-1 and headroom=1 means we have exactly 1 slot left
    check_budget(conn, "gemini-3.5-flash-lite", "2026-09-24")


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
    """Verify atomicity of record_call under true concurrent load.

    Uses a Barrier to release multiple threads into record_call at the same
    moment, each holding its own connection to the same database file. This
    closes the window where a naive SELECT-then-UPDATE would race and lose
    increments. Atomic upsert (INSERT...ON CONFLICT) must ensure final count
    equals total calls made.
    """
    path = tmp_path / "lore.db"
    open_db(path).close()  # migrate once, then open plain connections

    num_threads = 8
    calls_per_thread = 25
    total_calls = num_threads * calls_per_thread
    barrier = threading.Barrier(num_threads)

    def worker():
        # Each thread gets its own connection with timeout to handle contention
        conn = sqlite3.connect(path, timeout=30)
        try:
            barrier.wait()  # Release all threads at the same moment
            for _ in range(calls_per_thread):
                record_call(conn, "gemini-3.5-flash-lite", "2026-09-24")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        list(executor.map(lambda _: worker(), range(num_threads)))

    # Verify no increments were lost
    checker = sqlite3.connect(path)
    try:
        assert calls_today(checker, "gemini-3.5-flash-lite", "2026-09-24") == total_calls
    finally:
        checker.close()
