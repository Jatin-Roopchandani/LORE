from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.orchestrator.jobs import BookJob

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)  # 05:00 PDT


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "lore.db")
    c.execute("INSERT INTO books (id, title, file_path) VALUES ('b1','B','/x')")
    c.commit()
    yield c
    c.close()


def test_get_job_missing_is_none(conn) -> None:
    assert jobs.get_job(conn, "b1") is None
    assert jobs.list_jobs(conn) == []


def test_start_advance_finish(conn) -> None:
    j = jobs.start_job(conn, "b1", "extract", 10, now=NOW)
    assert (j.stage, j.state, j.done_items, j.total_items) == ("extract", "running", 0, 10)
    jobs.advance(conn, "b1", 4, now=NOW)
    assert jobs.get_job(conn, "b1").done_items == 4
    jobs.finish_stage(conn, "b1", "load", now=NOW)
    j = jobs.get_job(conn, "b1")
    assert (j.stage, j.state, j.done_items, j.total_items) == ("load", "idle", 0, 0)
    jobs.done(conn, "b1", now=NOW)
    j = jobs.get_job(conn, "b1")
    assert (j.stage, j.state) == ("ready", "done")
    assert [x.book_id for x in jobs.list_jobs(conn)] == ["b1"]


def test_start_job_clears_previous_stop(conn) -> None:
    jobs.start_job(conn, "b1", "extract", 5, now=NOW)
    jobs.stop_quota(conn, "b1", "limit", now=NOW)
    j = jobs.start_job(conn, "b1", "extract", 5, now=NOW)
    assert j.state == "running" and j.stopped_reason is None and j.resumable_at is None


def test_stop_quota_sets_reset(conn) -> None:
    jobs.start_job(conn, "b1", "extract", 5, now=NOW)
    jobs.stop_quota(conn, "b1", "limit hit", now=NOW)
    j = jobs.get_job(conn, "b1")
    assert j.state == "waiting_quota" and j.stopped_reason == "limit hit"
    # next midnight Pacific after 05:00 PDT 2026-10-28 is 2026-10-29 07:00 UTC
    assert j.resumable_at == datetime(2026, 10, 29, 7, 0, tzinfo=UTC)


def test_next_reset_utc_pst_and_boundaries() -> None:
    # PST (UTC-8): midnight Pacific is 08:00 UTC
    assert jobs.next_reset_utc(datetime(2026, 12, 1, 3, 0, tzinfo=UTC)) == \
        datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
    # exactly at midnight Pacific: the NEXT one
    assert jobs.next_reset_utc(datetime(2026, 12, 1, 8, 0, tzinfo=UTC)) == \
        datetime(2026, 12, 2, 8, 0, tzinfo=UTC)
    # the DST fall-back day is 25 hours long
    assert jobs.next_reset_utc(datetime(2026, 11, 1, 9, 0, tzinfo=UTC)) == \
        datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        jobs.next_reset_utc(datetime(2026, 12, 1, 3, 0))


def test_fail(conn) -> None:
    jobs.start_job(conn, "b1", "load", 3, now=NOW)
    jobs.fail(conn, "b1", "boom", now=NOW)
    j = jobs.get_job(conn, "b1")
    assert j.state == "failed" and j.stopped_reason == "boom" and j.resumable_at is None


def test_recover_stale(conn) -> None:
    conn.execute("INSERT INTO books (id, title, file_path) VALUES ('b2','B2','/y')")
    conn.commit()
    jobs.start_job(conn, "b1", "extract", 5, now=NOW)
    jobs.start_job(conn, "b2", "extract", 5, now=NOW + timedelta(seconds=100))
    later = NOW + timedelta(seconds=130)
    assert jobs.recover_stale(conn, later) == ["b1"]
    j1, j2 = jobs.get_job(conn, "b1"), jobs.get_job(conn, "b2")
    assert (j1.state, j1.stopped_reason) == ("idle", "interrupted")
    assert j2.state == "running"


def test_heartbeat_keeps_job_fresh(conn) -> None:
    jobs.start_job(conn, "b1", "extract", 5, now=NOW)
    jobs.heartbeat(conn, "b1", now=NOW + timedelta(seconds=110))
    assert jobs.recover_stale(conn, NOW + timedelta(seconds=200)) == []


def _job(**kw) -> BookJob:
    base = dict(book_id="b1", stage="extract", state="idle", done_items=0,
                total_items=0, stopped_reason=None, resumable_at=None, updated_at=NOW)
    base.update(kw)
    return BookJob(**base)


def test_can_continue() -> None:
    assert jobs.can_continue(None, NOW)[0] is True
    assert jobs.can_continue(_job(state="idle"), NOW)[0] is True
    assert jobs.can_continue(_job(state="failed"), NOW)[0] is True
    ok, why = jobs.can_continue(_job(state="done", stage="ready"), NOW)
    assert not ok and why
    reset = NOW + timedelta(hours=3)
    ok, why = jobs.can_continue(_job(state="waiting_quota", resumable_at=reset), NOW)
    assert not ok and "resets" in why
    assert jobs.can_continue(_job(state="waiting_quota", resumable_at=reset), reset)[0] is True
    ok, why = jobs.can_continue(_job(state="running", updated_at=NOW), NOW + timedelta(seconds=10))
    assert not ok and "running" in why
    # a stale running row does not block
    assert jobs.can_continue(_job(state="running", updated_at=NOW), NOW + timedelta(seconds=300))[0]


def test_move_target_is_saved_and_cleared_when_the_job_is_done(conn) -> None:
    jobs.start_job(conn, "b1", "remove", 1, now=NOW)
    assert jobs.move_target(conn, "b1") is None
    jobs.set_move_target(conn, "b1", 7)
    assert jobs.move_target(conn, "b1") == 7
    jobs.fail(conn, "b1", "x", now=NOW)
    assert jobs.move_target(conn, "b1") == 7  # a failed move stays a move
    jobs.done(conn, "b1", now=NOW)
    assert jobs.move_target(conn, "b1") is None
    assert jobs.move_target(conn, "nope") is None
