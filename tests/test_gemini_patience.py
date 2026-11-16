"""A patient Gemini client (book processing): per-minute 429s and transport
failures are ridden out within the request; only a daily exhaustion, or
patience running out, reaches the caller, saying which it was.

No network: a stubbed transport, a fake monotonic clock and a fake sleep.
"""
import json
from datetime import datetime, timezone

import httpx
import pytest

from lore.inference.gemini import (
    LITE_MODELS,
    TRANSPORT_BACKOFF,
    GeminiClient,
    GeminiError,
    GeminiQuotaError,
    GeminiServerError,
    GeminiTransportError,
    parse_rate_limit,
    seconds_to_reset,
)

SECRET = "AIza-not-a-real-key-0000"
NOON_UTC = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)  # 04:00 Pacific


def _ok(text: str = "ok") -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _model_of(request: httpx.Request) -> str:
    return str(request.url).rsplit("/", 1)[-1].split(":")[0]


def _quota_body(quota_id: str | None, delay: str | None = "37s") -> str:
    details = []
    if quota_id is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaMetric": "generativelanguage.googleapis.com/"
                                        "generate_content_free_tier_requests",
                                        "quotaId": quota_id}]})
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": delay})
    return json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                 "message": "quota", "details": details}})


MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


def _client(handler, clock: list[float], slept: list[float] | None = None,
            **kwargs) -> GeminiClient:
    async def sleep(seconds: float) -> None:
        if slept is not None:
            slept.append(seconds)
        clock[0] += seconds

    kwargs.setdefault("patience", 600.0)
    return GeminiClient(SECRET, transport=httpx.MockTransport(handler),
                        models=list(LITE_MODELS), sleep=sleep,
                        now=lambda: clock[0], wall_clock=lambda: NOON_UTC, **kwargs)


# --- reading a 429 ------------------------------------------------------------

def test_parse_reads_a_per_minute_quota_and_its_retry_delay() -> None:
    said = parse_rate_limit(_quota_body(MINUTE, "37s"))
    assert said.daily is False and said.retry_after == 37.0


def test_parse_reads_a_daily_quota_even_with_a_short_retry_delay() -> None:
    said = parse_rate_limit(_quota_body(DAY, "20s"))
    assert said.daily is True and said.retry_after == 20.0


def test_parse_a_daily_quota_wins_over_a_per_minute_one_beside_it() -> None:
    body = json.loads(_quota_body(MINUTE))
    body["error"]["details"][0]["violations"].append({"quotaId": DAY})
    assert parse_rate_limit(json.dumps(body)).daily is True


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"error": "x"}',
                                  '{"error": {"details": [1, {"@type": 3}]}}'])
def test_parse_never_raises_on_a_body_that_does_not_say(body: str) -> None:
    assert parse_rate_limit(body).daily is None


def test_parse_falls_back_to_the_retry_after_header() -> None:
    said = parse_rate_limit("{}", httpx.Headers({"retry-after": "12"}))
    assert said.daily is None and said.retry_after == 12.0


def test_seconds_to_reset_is_midnight_pacific() -> None:
    # 12:00 UTC on 16 Nov is 04:00 PST: 20 hours to midnight Pacific.
    assert seconds_to_reset(NOON_UTC) == 20 * 3600


# --- per-minute limits are waited out ------------------------------------------

@pytest.mark.asyncio
async def test_a_per_minute_429_on_every_model_waits_and_carries_on() -> None:
    clock, slept, calls = [0.0], [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((_model_of(request), clock[0]))
        if clock[0] < 30:
            return httpx.Response(429, text=_quota_body(MINUTE, "30s"))
        return httpx.Response(200, json=_ok())

    client = _client(handler, clock, slept)
    assert await client.generate("x") == "ok"
    assert slept == [30.0]  # waited for the soonest model, once
    assert [m for m, _ in calls] == [LITE_MODELS[0], LITE_MODELS[1], LITE_MODELS[0]]


@pytest.mark.asyncio
async def test_a_daily_429_on_every_model_is_raised_as_daily_without_waiting() -> None:
    clock, slept = [0.0], []
    client = _client(lambda r: httpx.Response(429, text=_quota_body(DAY)), clock, slept)
    with pytest.raises(GeminiQuotaError) as caught:
        await client.generate("x")
    assert caught.value.daily is True
    assert slept == []
    # Out for the day: the next call spends no request at all.
    calls = client.requests_made
    with pytest.raises(GeminiQuotaError) as again:
        await client.generate("y")
    assert again.value.daily is True and client.requests_made == calls


@pytest.mark.asyncio
async def test_per_minute_limits_past_patience_are_raised_as_short() -> None:
    clock = [0.0]
    client = _client(lambda r: httpx.Response(429, text=_quota_body(MINUTE, "60s")),
                     clock, patience=100.0)
    with pytest.raises(GeminiQuotaError) as caught:
        await client.generate("x")
    assert caught.value.daily is False
    assert caught.value.retry_after is not None and caught.value.retry_after <= 60


@pytest.mark.asyncio
async def test_one_model_daily_and_the_other_per_minute_waits_for_the_other() -> None:
    clock, slept = [0.0], []

    def handler(request: httpx.Request) -> httpx.Response:
        if _model_of(request) == LITE_MODELS[0]:
            return httpx.Response(429, text=_quota_body(DAY))
        if clock[0] < 45:
            return httpx.Response(429, text=_quota_body(MINUTE, "45s"))
        return httpx.Response(200, json=_ok())

    client = _client(handler, clock, slept)
    assert await client.generate("x") == "ok"
    assert slept == [45.0] and client.last_model == LITE_MODELS[1]


# --- a 429 that does not say: LORE's ledger decides ----------------------------

@pytest.mark.asyncio
async def test_an_unexplained_429_well_under_the_daily_count_is_per_minute() -> None:
    clock, slept = [0.0], []

    def handler(request: httpx.Request) -> httpx.Response:
        if clock[0] < 60:
            return httpx.Response(429, text="Resource has been exhausted")
        return httpx.Response(200, json=_ok())

    client = _client(handler, clock, slept, daily_count=lambda model: 140)
    assert await client.generate("x") == "ok"
    assert slept == [60.0]


@pytest.mark.asyncio
async def test_an_unexplained_429_near_the_daily_count_is_daily() -> None:
    clock, slept = [0.0], []
    client = _client(lambda r: httpx.Response(429, text="Resource has been exhausted"),
                     clock, slept, daily_count=lambda model: 495)
    with pytest.raises(GeminiQuotaError) as caught:
        await client.generate("x")
    assert caught.value.daily is True and slept == []


@pytest.mark.asyncio
async def test_with_no_ledger_and_nothing_said_a_429_stays_daily() -> None:
    clock = [0.0]
    client = _client(lambda r: httpx.Response(429, text="nope"), clock)
    with pytest.raises(GeminiQuotaError) as caught:
        await client.generate("x")
    assert caught.value.daily is True


# --- transport failures and server errors are retried --------------------------

@pytest.mark.asyncio
async def test_a_transport_failure_is_retried_with_backoff_then_succeeds() -> None:
    clock, slept, tries = [0.0], [], [0]

    def handler(request: httpx.Request) -> httpx.Response:
        tries[0] += 1
        if tries[0] <= 2:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json=_ok())

    client = _client(handler, clock, slept)
    assert await client.generate("x") == "ok"
    assert slept == list(TRANSPORT_BACKOFF[:2])


@pytest.mark.asyncio
async def test_a_transport_failure_that_persists_is_raised_after_the_backoff() -> None:
    clock, slept = [0.0], []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = _client(handler, clock, slept)
    with pytest.raises(GeminiTransportError):
        await client.generate("x")
    assert slept == list(TRANSPORT_BACKOFF)
    assert client.requests_made == len(TRANSPORT_BACKOFF) + 1
    assert sum(slept) <= 180  # a couple of minutes, not more


@pytest.mark.parametrize("status", [500, 502, 504])
@pytest.mark.asyncio
async def test_a_server_error_is_retried_then_raised_as_a_server_error(status: int) -> None:
    clock, slept = [0.0], []
    client = _client(lambda r: httpx.Response(status, text="internal"), clock, slept)
    with pytest.raises(GeminiServerError) as caught:
        await client.generate("x")
    assert f"HTTP {status}" in str(caught.value)
    assert slept == list(TRANSPORT_BACKOFF)


# --- an impatient client (questions) behaves exactly as before ------------------

@pytest.mark.asyncio
async def test_an_impatient_client_never_sleeps_nor_retries_transport() -> None:
    clock, slept = [0.0], []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _client(handler, clock, slept, patience=0.0)
    with pytest.raises(GeminiTransportError):
        await client.generate("x")
    assert slept == [] and client.requests_made == 1


@pytest.mark.asyncio
async def test_an_impatient_client_raises_a_per_minute_429_at_once() -> None:
    clock, slept = [0.0], []
    client = _client(lambda r: httpx.Response(429, text=_quota_body(MINUTE)),
                     clock, slept, patience=0.0)
    with pytest.raises(GeminiQuotaError) as caught:
        await client.generate("x")
    assert slept == [] and caught.value.daily is False
    assert "out of free-tier budget;" in str(caught.value)  # the message as before


@pytest.mark.asyncio
async def test_an_impatient_server_error_is_still_a_plain_gemini_error() -> None:
    clock = [0.0]
    client = _client(lambda r: httpx.Response(500, text="boom"), clock, patience=0.0)
    with pytest.raises(GeminiError) as caught:
        await client.generate("x")
    assert str(caught.value) == "HTTP 500 from gemini-3.1-flash-lite: boom"
