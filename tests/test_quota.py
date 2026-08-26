from datetime import datetime, timezone
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.orchestrator.quota import (
    QuotaExceeded,
    check_budget,
    hours_used,
    iso_week,
    record_actual,
    record_dispatch,
    week_report,
)


def test_iso_week_formats_year_and_week() -> None:
    assert iso_week(datetime(2026, 8, 25, tzinfo=timezone.utc)) == "2026-W35"


def test_hours_used_starts_at_zero(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    assert hours_used(conn, "2026-W35") == 0.0
    conn.close()


def test_estimate_counts_until_actual_replaces_it(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=3.0, week="2026-W35")
    assert hours_used(conn, "2026-W35") == 3.0
    record_actual(conn, "run-1", actual_hours=1.5)
    assert hours_used(conn, "2026-W35") == 1.5
    conn.close()


def test_other_weeks_do_not_count(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=5.0, week="2026-W34")
    assert hours_used(conn, "2026-W35") == 0.0
    conn.close()


def test_check_budget_allows_within_limit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=20.0, week="2026-W35")
    check_budget(conn, est_hours=5.0, week="2026-W35")  # 25 <= 30, no raise
    conn.close()


def test_check_budget_refuses_over_limit(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=28.0, week="2026-W35")
    with pytest.raises(QuotaExceeded, match="30"):
        check_budget(conn, est_hours=5.0, week="2026-W35")
    conn.close()


def test_week_report_summarizes(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "lore.db")
    record_dispatch(conn, "run-1", est_hours=2.0, week="2026-W35")
    record_actual(conn, "run-1", actual_hours=1.0)
    record_dispatch(conn, "run-2", est_hours=4.0, week="2026-W35")
    report = week_report(conn, "2026-W35")
    assert report["week"] == "2026-W35"
    assert report["used_hours"] == 5.0
    assert report["remaining_hours"] == 25.0
    assert report["runs"] == 2
    conn.close()
