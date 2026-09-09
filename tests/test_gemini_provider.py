"""The Gemini free-tier cascade, entirely against a stubbed transport.

No test here may reach the network. The Lite tier allows 500 requests a
day and the Flash tier 20, and the controller owns both budgets.
"""
import json

import httpx
import pytest

from lore.inference.gemini import (
    DEFAULT_MODELS,
    ENDPOINT,
    MODEL_LIMITS,
    GeminiClient,
    GeminiError,
    GeminiQuotaError,
    limits_for,
    resolve_api_key,
)

SECRET = "AIza-not-a-real-key-0000"


def _ok(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _client(handler, **kwargs) -> GeminiClient:
    return GeminiClient(
        SECRET, transport=httpx.MockTransport(handler), **kwargs
    )


def _model_of(request: httpx.Request) -> str:
    return str(request.url).rsplit("/", 1)[-1].split(":")[0]


@pytest.mark.asyncio
async def test_generate_uses_the_goog_api_key_header_and_documented_endpoint() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok("1. A -> none"))

    client = _client(handler)
    assert await client.generate("hello") == "1. A -> none"
    # A Bearer token returns 401 against this API — verified in the brief.
    assert seen["headers"]["x-goog-api-key"] == SECRET
    assert "authorization" not in seen["headers"]
    assert seen["url"] == ENDPOINT.format(model=DEFAULT_MODELS[0])
    assert ":generateContent" in seen["url"]
    assert seen["body"]["generationConfig"]["temperature"] == 0
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "hello"


def test_the_cascade_is_ordered_by_measured_score_and_quota() -> None:
    # NOT by version number. gemini-3.1-flash-lite scored 12/12 on the
    # 12-case eval and 3.5-flash-lite 11/12, and both Lite models carry
    # 500 RPD against Flash's 20 — so the older Lite leads. 3.8-flash was
    # correct but threw repeated 503s, so it sits ahead of 3.7-flash only
    # because the cascade falls through a 503 for free.
    assert DEFAULT_MODELS == (
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.7-flash",
    )
    # 404 on this key — listing either would burn a cascade step on nothing.
    assert "gemini-2.5-flash" not in DEFAULT_MODELS
    assert "gemini-2.5-flash-lite" not in DEFAULT_MODELS
    assert limits_for("gemini-3.1-flash-lite").rpd == 500
    assert limits_for("gemini-3.1-flash-lite").rpm == 15
    assert limits_for("gemini-3.7-flash").rpd == 20
    assert limits_for("gemini-3.7-flash").rpm == 5
    # An unlisted id guesses low rather than talking us past a real ceiling.
    assert limits_for("gemini-nonesuch").rpd == 20
    assert set(DEFAULT_MODELS) <= set(MODEL_LIMITS)
    # ~1100 requests/day of aggregate headroom for a workload needing one.
    assert sum(limits_for(m).rpd for m in DEFAULT_MODELS) > 1000


@pytest.mark.asyncio
async def test_the_model_list_is_configurable() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_model_of(request))
        return httpx.Response(200, json=_ok("ok"))

    client = _client(handler, models=["gemini-3.8-flash"])
    await client.generate("hi")
    assert seen == ["gemini-3.8-flash"]
    assert client.last_model == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_multi_part_text_is_concatenated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "1. A -> 1\n"},
                                                  {"text": "2. B -> 2"}]}}]
        })

    assert await _client(handler).generate("p") == "1. A -> 1\n2. B -> 2"


@pytest.mark.parametrize("status", [429, 503, 404])
@pytest.mark.asyncio
async def test_a_refusing_model_falls_through_to_the_next_one(status: int) -> None:
    # Cascade, not backoff: a per-model daily quota is escaped by moving
    # to another model, never by sleeping through it. 503s were observed
    # moving between models minute to minute.
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = _model_of(request)
        tried.append(model)
        if model == DEFAULT_MODELS[0]:
            return httpx.Response(status, text="nope")
        return httpx.Response(200, json=_ok("1. A -> 1"))

    slept: list[float] = []

    async def sleep(seconds: float) -> None:  # pragma: no cover
        slept.append(seconds)

    client = _client(handler, sleep=sleep)
    assert await client.generate("p") == "1. A -> 1"
    assert tried == [DEFAULT_MODELS[0], DEFAULT_MODELS[1]]
    assert client.last_model == DEFAULT_MODELS[1]
    assert client.requests_made == 2
    assert slept == [], "a refusal moves on rather than waiting"


@pytest.mark.asyncio
async def test_an_exhausted_cascade_names_every_model_it_tried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "RESOURCE_EXHAUSTED"}})

    client = _client(handler)
    with pytest.raises(GeminiQuotaError) as exc:
        await client.generate("p")
    message = str(exc.value)
    for model in DEFAULT_MODELS:
        assert model in message
    assert "cache" in message
    assert SECRET not in message
    assert client.requests_made == len(DEFAULT_MODELS)


@pytest.mark.asyncio
async def test_a_rejected_key_is_fatal_and_never_cascades() -> None:
    # Every model would reject the same key; trying four more only wastes
    # time. And the message must never echo the key itself.
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_model_of(request))
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    client = _client(handler)
    with pytest.raises(GeminiError) as exc:
        await client.generate("p")
    assert not isinstance(exc.value, GeminiQuotaError)
    assert len(tried) == 1
    assert SECRET not in str(exc.value)
    assert "GEMINI_API_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_empty_candidate_list_is_an_error_not_an_empty_answer() -> None:
    # An empty string parsed as "no answers" would look like a short answer
    # set; naming it here keeps the diagnosis at the provider seam.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [],
                                         "promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(GeminiError) as exc:
        await _client(handler).generate("p")
    assert "SAFETY" in str(exc.value)


@pytest.mark.asyncio
async def test_a_second_call_to_one_model_respects_that_models_rpm() -> None:
    # Batching normally means one request. If a batch is ever split, the
    # per-model RPM ceiling still applies — 15 RPM for Lite, so 4s.
    clock = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("ok"))

    client = _client(handler, sleep=sleep, now=lambda: clock[0])
    await client.generate("one")
    assert slept == [], "the first request waits for nothing"
    await client.generate("two")
    assert slept == [pytest.approx(60.0 / limits_for(DEFAULT_MODELS[0]).rpm)]
    assert client.requests_made == 2


@pytest.mark.asyncio
async def test_a_request_that_already_waited_long_enough_does_not_sleep() -> None:
    clock = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:  # pragma: no cover
        slept.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("ok"))

    client = _client(handler, sleep=sleep, now=lambda: clock[0])
    await client.generate("one")
    clock[0] = 999.0
    await client.generate("two")
    assert slept == []


@pytest.mark.asyncio
async def test_daily_limit_reports_the_model_that_answered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        model = _model_of(request)
        if model != "gemini-3.7-flash":
            return httpx.Response(429, text="nope")
        return httpx.Response(200, json=_ok("ok"))

    client = _client(handler)
    await client.generate("p")
    assert client.last_model == "gemini-3.7-flash"
    assert client.daily_limit == 20


def test_an_empty_model_list_is_refused() -> None:
    with pytest.raises(ValueError):
        GeminiClient(SECRET, models=[])


def test_resolve_api_key_prefers_gemini_then_google_then_none() -> None:
    assert resolve_api_key({"GEMINI_API_KEY": "a", "GOOGLE_API_KEY": "b"}) == "a"
    assert resolve_api_key({"GOOGLE_API_KEY": "b"}) == "b"
    assert resolve_api_key({}) is None
    assert resolve_api_key({"GEMINI_API_KEY": "   "}) is None
