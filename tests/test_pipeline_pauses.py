"""How a stopped book is parked: a provider that could not be reached and a
per-minute limit pause it for a few minutes (never `failed`); only a daily
exhaustion waits for the reset; a real failure still fails."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.extract.runner import ExtractionHalted
from lore.inference.gemini import (
    GeminiAuthError,
    GeminiQuotaError,
    GeminiServerError,
    GeminiTransportError,
)
from lore.inference.providers.base import ProviderQuotaError, ProviderTransportError
from lore.orchestrator import jobs, pipeline
from lore.orchestrator.pipeline import QuotaStop, run_book

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
RESET = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)  # midnight PST


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "lore.db")
    c.execute("INSERT INTO books (id, title, file_path) VALUES ('b1','B','/x')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def config(tmp_path: Path) -> LoreConfig:
    return LoreConfig(data_dir=tmp_path)


class Stage:
    """Extract: items 1..4; raises `error()` once it reaches `at`."""

    name = "extract"

    def __init__(self, error=None, at=3):
        self.error, self.at, self.done = error, at, set()

    def total(self, ctx):
        return 4

    def run(self, ctx, job_ctl):
        job_ctl.advance(len(self.done))
        for i in (1, 2, 3, 4):
            if i in self.done:
                continue
            if self.error is not None and i == self.at:
                raise self.error()
            self.done.add(i)
            job_ctl.advance(len(self.done))


def _as_extraction_reports_it(cause):
    """The chain a unit's halt really has: RuntimeError from the unit,
    from ExtractionHalted, from the client's error."""
    def make():
        try:
            try:
                raise cause
            except Exception as exc:
                raise ExtractionHalted(f"Gemini cannot extract right now: {exc}") from exc
        except ExtractionHalted as halted:
            try:
                raise RuntimeError(str(halted)) from halted
            except RuntimeError as outer:
                return outer
    return make


def _quota_stop(cause):
    def make():
        try:
            raise cause
        except Exception as exc:
            try:
                raise QuotaStop(f"Gemini has no model left to serve this: {exc}") from exc
            except QuotaStop as stop:
                return stop
    return make


def test_unreachable_gemini_pauses_for_minutes_instead_of_failing(conn, config) -> None:
    cause = GeminiTransportError("could not reach the Gemini API: [Errno -3]")
    job = run_book(conn, config, "b1", [Stage(_as_extraction_reports_it(cause))], now=NOW)
    assert job.state == jobs.WAITING_QUOTA
    assert job.retries == 1 and job.short_pause and job.auto_resumes
    assert job.resumable_at == NOW + pipeline.FIRST_SHORT_PAUSE
    assert job.stopped_reason.startswith("Paused: LORE couldn't reach Gemini; it will"
                                         " retry in about 2 minutes")
    assert "extract: 2/4" in job.stopped_reason
    # Continue is "now" for a short pause.
    assert jobs.can_continue(job, NOW + timedelta(seconds=5)) == (True, "")


@pytest.mark.parametrize("cause", [GeminiServerError("HTTP 500 from m: internal"),
                                   ProviderTransportError("no route", provider="anthropic")])
def test_a_server_error_or_another_providers_transport_failure_pauses_too(
        conn, config, cause) -> None:
    job = run_book(conn, config, "b1", [Stage(_as_extraction_reports_it(cause))], now=NOW)
    assert job.state == jobs.WAITING_QUOTA and job.retries == 1


def test_a_per_minute_limit_pauses_briefly_not_until_the_reset(conn, config) -> None:
    cause = GeminiQuotaError("every configured model refused the request — HTTP 429",
                             daily=False, retry_after=40.0)
    job = run_book(conn, config, "b1", [Stage(_quota_stop(cause))], now=NOW)
    assert job.state == jobs.WAITING_QUOTA and job.retries == 1
    assert job.resumable_at == NOW + pipeline.FIRST_SHORT_PAUSE
    assert "per-minute limit" in job.stopped_reason and "429" in job.stopped_reason


def test_a_long_retry_after_is_respected(conn, config) -> None:
    cause = ProviderQuotaError("HTTP 429 from claude: rate limited (retry after 900s).",
                               provider="anthropic", retry_after=900.0)
    job = run_book(conn, config, "b1", [Stage(_quota_stop(cause))], now=NOW)
    assert job.resumable_at == NOW + timedelta(seconds=900)
    assert job.stopped_reason.startswith("Paused: Claude is limiting")


def test_a_daily_exhaustion_waits_for_the_reset_in_plain_words(conn, config) -> None:
    cause = GeminiQuotaError("every configured model refused the request — HTTP 429"
                             " (daily limit)", daily=True)
    job = run_book(conn, config, "b1", [Stage(_quota_stop(cause))], now=NOW)
    assert job.state == jobs.WAITING_QUOTA and job.retries == 0
    assert job.resumable_at == RESET and job.auto_resumes and not job.short_pause
    assert job.stopped_reason.startswith(
        "Paused: Gemini's free-tier daily limit is used up. It resets at midnight"
        " Pacific (≈ 08:00 UTC)")
    ok, why = jobs.can_continue(job, NOW + timedelta(hours=1))
    assert not ok and "resets" in why


def test_the_reserve_keeps_its_own_words(conn, config) -> None:
    job = run_book(conn, config, "b1",
                   [Stage(lambda: QuotaStop("Gemini processing reserve reached"))], now=NOW)
    assert job.stopped_reason.startswith("Gemini processing reserve reached (during")
    assert job.resumable_at == RESET and job.retries == 0


def test_a_rejected_key_or_a_bug_still_fails(conn, config) -> None:
    job = run_book(conn, config, "b1",
                   [Stage(_as_extraction_reports_it(GeminiAuthError("HTTP 401")))], now=NOW)
    assert job.state == jobs.FAILED
    conn.execute("DELETE FROM book_jobs")
    job = run_book(conn, config, "b1", [Stage(lambda: ValueError("malformed"))], now=NOW)
    assert job.state == jobs.FAILED and "malformed" in job.stopped_reason


def test_short_pauses_with_no_progress_grow_then_wait_for_continue(conn, config) -> None:
    cause = GeminiTransportError("down")
    stage = Stage(_as_extraction_reports_it(cause))
    now = NOW
    waits = []
    for n in range(1, pipeline.MAX_SHORT_PAUSES + 1):
        job = run_book(conn, config, "b1", [stage], now=now)
        assert job.state == jobs.WAITING_QUOTA and job.retries == n
        waits.append(job.resumable_at - now)
        now = job.resumable_at
    assert waits[:3] == [timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=8)]
    assert max(waits) == pipeline.LONGEST_SHORT_PAUSE
    job = run_book(conn, config, "b1", [stage], now=now)
    assert job.state == jobs.IDLE and job.resumable_at is None and job.retries == 0
    assert "after several tries. Press Continue" in job.stopped_reason
    assert not job.auto_resumes


def test_progress_between_short_pauses_starts_the_count_again(conn, config) -> None:
    stage = Stage(_as_extraction_reports_it(GeminiTransportError("down")), at=2)
    job = run_book(conn, config, "b1", [stage], now=NOW)
    job = run_book(conn, config, "b1", [stage], now=job.resumable_at)
    assert job.retries == 2
    stage.at = 4  # items 2 and 3 get done this time
    job = run_book(conn, config, "b1", [stage], now=job.resumable_at)
    assert job.retries == 1


def test_a_book_that_finishes_clears_its_retries(conn, config) -> None:
    stage = Stage(_as_extraction_reports_it(GeminiTransportError("down")))
    job = run_book(conn, config, "b1", [stage], now=NOW)
    stage.error = None
    job = run_book(conn, config, "b1", [stage], now=job.resumable_at)
    assert job.state == jobs.DONE and job.retries == 0
