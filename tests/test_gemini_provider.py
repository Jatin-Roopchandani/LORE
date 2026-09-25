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


# --- structured generation (lore ask's opt-in synthesis) -------------------

from pydantic import BaseModel  # noqa: E402

from lore.inference.gemini import (  # noqa: E402
    LITE_MODELS,
    GeminiKeyFileError,
    default_key_file,
)


class _Pick(BaseModel):
    name: str
    count: int


@pytest.mark.asyncio
async def test_generate_json_asks_for_json_and_validates_the_schema() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok('{"name": "Holmes", "count": 2}'))

    client = _client(handler)
    result = await client.generate_json("who?", _Pick)
    assert result == _Pick(name="Holmes", count=2)
    config = seen["body"]["generationConfig"]
    assert config == {"temperature": 0, "responseMimeType": "application/json"}
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert prompt.startswith("who?")
    assert "Respond with ONLY a JSON object (no prose, no code fences)" in prompt
    assert json.dumps(_Pick.model_json_schema()) in prompt
    assert client.last_model == DEFAULT_MODELS[0]


@pytest.mark.parametrize("fenced", [
    '```json\n{"name": "Holmes", "count": 2}\n```',
    '```\n{"name": "Holmes", "count": 2}\n```\n',
])
@pytest.mark.asyncio
async def test_generate_json_strips_a_code_fence(fenced: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(fenced))

    assert await _client(handler).generate_json("p", _Pick) == _Pick(name="Holmes", count=2)


@pytest.mark.parametrize("bad", ["not json at all", '{"name": "Holmes"}'])
@pytest.mark.asyncio
async def test_generate_json_names_invalid_output_clearly(bad: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(bad))

    with pytest.raises(GeminiError) as exc:
        await _client(handler).generate_json("p", _Pick)
    message = str(exc.value)
    assert DEFAULT_MODELS[0] in message
    assert "_Pick" in message
    assert SECRET not in message


@pytest.mark.asyncio
async def test_generate_json_cascades_past_a_429() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_model_of(request))
        if len(tried) == 1:
            return httpx.Response(429, text="nope")
        return httpx.Response(200, json=_ok('{"name": "W", "count": 1}'))

    client = _client(handler, models=list(LITE_MODELS))
    assert await client.generate_json("p", _Pick) == _Pick(name="W", count=1)
    assert tried == list(LITE_MODELS)
    assert client.last_model == LITE_MODELS[1]


@pytest.mark.asyncio
async def test_generate_json_treats_a_rejected_key_as_fatal() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_model_of(request))
        return httpx.Response(403, text="denied")

    with pytest.raises(GeminiError) as exc:
        await _client(handler).generate_json("p", _Pick)
    assert not isinstance(exc.value, GeminiQuotaError)
    assert len(tried) == 1


@pytest.mark.asyncio
async def test_an_exhausted_json_cascade_does_not_blame_the_disambiguator() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope")

    with pytest.raises(GeminiQuotaError) as exc:
        await _client(handler, models=list(LITE_MODELS)).generate_json("p", _Pick)
    message = str(exc.value)
    assert all(model in message for model in LITE_MODELS)
    assert "disambiguate" not in message


def test_the_lite_models_lead_the_default_cascade() -> None:
    assert LITE_MODELS == ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite")
    assert LITE_MODELS == DEFAULT_MODELS[:2]


def _key_file(tmp_path, content: str, mode: int):
    path = tmp_path / "gemini.env"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_the_environment_wins_over_the_key_file(tmp_path) -> None:
    path = _key_file(tmp_path, "GEMINI_API_KEY=from-file\n", 0o600)
    assert resolve_api_key({"GEMINI_API_KEY": "from-env"}, key_file=path) == "from-env"
    assert resolve_api_key({"GOOGLE_API_KEY": "google"}, key_file=path) == "google"


def test_the_key_file_is_read_when_the_environment_has_none(tmp_path) -> None:
    path = _key_file(tmp_path, "# a comment\n\nGEMINI_API_KEY = 'from-file'\n", 0o600)
    assert resolve_api_key({}, key_file=path) == "from-file"
    quoted = _key_file(tmp_path, 'GEMINI_API_KEY="dq"\n', 0o400)
    assert resolve_api_key({}, key_file=quoted) == "dq"


def test_a_missing_or_keyless_file_is_no_key(tmp_path) -> None:
    assert resolve_api_key({}, key_file=tmp_path / "absent.env") is None
    path = _key_file(tmp_path, "OTHER=1\nGEMINI_API_KEY=\n", 0o600)
    assert resolve_api_key({}, key_file=path) is None
    # No file argument: environment only, exactly as before.
    assert resolve_api_key({}) is None


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666])
def test_a_key_file_others_can_read_is_refused_and_says_why(tmp_path, mode: int) -> None:
    path = _key_file(tmp_path, f"GEMINI_API_KEY={SECRET}\n", mode)
    with pytest.raises(GeminiKeyFileError) as exc:
        resolve_api_key({}, key_file=path)
    message = str(exc.value)
    assert str(path) in message
    assert oct(mode)[2:] in message
    assert "chmod 600" in message
    assert SECRET not in message


def test_the_default_key_file_lives_outside_the_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_key_file() == tmp_path / ".config" / "lore" / "gemini.env"


# --- Task R1: error classes a caller can act on -----------------------------

from lore.inference.gemini import (  # noqa: E402
    GeminiAuthError,
    GeminiOutputError,
    GeminiTransportError,
)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_a_rejected_key_is_a_gemini_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="denied")

    with pytest.raises(GeminiAuthError) as exc:
        await _client(handler).generate_json("p", _Pick)
    assert SECRET not in str(exc.value)


@pytest.mark.asyncio
async def test_a_network_failure_is_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(GeminiTransportError):
        await _client(handler).generate("p")


@pytest.mark.parametrize("body", [
    {"candidates": []},
    {"candidates": [{"content": {"parts": [{"text": "   "}]}}]},
    _ok("not json"),
])
@pytest.mark.asyncio
async def test_bad_model_output_is_an_output_error_carrying_the_raw_text(body) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(GeminiOutputError) as exc:
        await _client(handler).generate_json("p", _Pick)
    assert not isinstance(exc.value, GeminiQuotaError)
    assert isinstance(exc.value.raw_output, str)


@pytest.mark.asyncio
async def test_the_output_error_keeps_the_raw_model_text_for_dead_letters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("{broken"))

    with pytest.raises(GeminiOutputError) as exc:
        await _client(handler).generate_json("p", _Pick)
    assert exc.value.raw_output == "{broken"


@pytest.mark.asyncio
async def test_the_exhausted_hint_is_the_callers_to_choose() -> None:
    # Task 8b finding 3: the JSON cascade's hint named only [lore.synthesis],
    # which is wrong advice for extraction and adjudication.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope")

    client = _client(handler, models=list(LITE_MODELS))
    with pytest.raises(GeminiQuotaError) as exc:
        await client.generate_json("p", _Pick, exhausted_hint="rerun with --resume")
    assert "rerun with --resume" in str(exc.value)
    assert "[lore.synthesis]" not in str(exc.value)
    with pytest.raises(GeminiQuotaError) as exc:
        await client.generate("p", exhausted_hint="adjudication hint")
    assert "adjudication hint" in str(exc.value)
    assert "disambiguate" not in str(exc.value)


# --- Task 10f: a refusing model cools down instead of being re-spent ----------

from lore.inference.gemini import COOLDOWN_SECONDS  # noqa: E402


def _refusing_first(status: int, tried: list[str], refuse: set[str] | None = None):
    refusers = refuse if refuse is not None else {LITE_MODELS[0]}

    def handler(request: httpx.Request) -> httpx.Response:
        model = _model_of(request)
        tried.append(model)
        if model in refusers:
            return httpx.Response(status, text="nope")
        return httpx.Response(200, json=_ok("ok"))
    return handler


def _clocked(handler, clock: list[float], **kwargs) -> GeminiClient:
    async def sleep(seconds: float) -> None:
        clock[0] += seconds

    return _client(handler, models=list(LITE_MODELS), sleep=sleep,
                   now=lambda: clock[0], **kwargs)


def test_the_cooldown_is_a_modest_fixed_minute() -> None:
    # Long enough that a per-minute RPM refusal has cleared when it ends;
    # short enough that a daily-quota refusal costs one wasted request a
    # minute at most, instead of one per call.
    assert COOLDOWN_SECONDS == 60.0


@pytest.mark.parametrize("status", [429, 503, 404])
@pytest.mark.asyncio
async def test_a_refusing_model_is_skipped_within_its_cooldown(status: int) -> None:
    clock = [0.0]
    tried: list[str] = []
    client = _clocked(_refusing_first(status, tried), clock)
    await client.generate("one")
    assert tried == [LITE_MODELS[0], LITE_MODELS[1]]
    tried.clear()
    clock[0] += COOLDOWN_SECONDS - 1
    await client.generate("two")
    assert tried == [LITE_MODELS[1]], "the refusing model is not re-spent"
    assert client.last_model == LITE_MODELS[1]
    assert client.attempts == {LITE_MODELS[0]: 1, LITE_MODELS[1]: 2}
    assert client.requests_made == 3


@pytest.mark.asyncio
async def test_a_cooled_model_is_tried_again_once_its_cooldown_ends() -> None:
    clock = [0.0]
    tried: list[str] = []
    refusers = {LITE_MODELS[0]}
    client = _clocked(_refusing_first(429, tried, refusers), clock)
    await client.generate("one")
    refusers.clear()  # its budget came back
    tried.clear()
    clock[0] += COOLDOWN_SECONDS
    assert await client.generate("two") == "ok"
    assert tried == [LITE_MODELS[0]]
    assert client.last_model == LITE_MODELS[0]


@pytest.mark.asyncio
async def test_a_model_that_refuses_again_after_its_cooldown_cools_again() -> None:
    clock = [0.0]
    tried: list[str] = []
    client = _clocked(_refusing_first(429, tried), clock)
    await client.generate("one")
    clock[0] += COOLDOWN_SECONDS
    await client.generate("two")
    tried.clear()
    clock[0] += 1
    await client.generate("three")
    assert tried == [LITE_MODELS[1]]


@pytest.mark.asyncio
async def test_when_every_model_is_cooling_the_soonest_to_end_is_tried() -> None:
    clock = [0.0]
    tried: list[str] = []
    refusers = set(LITE_MODELS)
    client = _clocked(_refusing_first(429, tried, refusers), clock)
    with pytest.raises(GeminiQuotaError):
        await client.generate("one")      # both cool at t=0 -> until 60
    clock[0] = 10.0
    tried.clear()
    with pytest.raises(GeminiQuotaError):
        await client.generate("two")      # all cooling: tie -> cascade order
    assert tried == [LITE_MODELS[0]]      # 0 re-cools at t=10 -> until 70
    tried.clear()
    refusers.clear()
    clock[0] = 20.0
    assert await client.generate("three") == "ok"
    assert tried == [LITE_MODELS[1]], "the model whose cooldown ends first"
    assert client.last_model == LITE_MODELS[1]


@pytest.mark.asyncio
async def test_all_cooling_and_the_soonest_refuses_raises_the_quota_error() -> None:
    clock = [0.0]
    tried: list[str] = []
    client = _clocked(_refusing_first(429, tried, set(LITE_MODELS)), clock)
    with pytest.raises(GeminiQuotaError):
        await client.generate("one")
    tried.clear()
    clock[0] += 5
    with pytest.raises(GeminiQuotaError) as exc:
        await client.generate("two", exhausted_hint="come back later")
    assert tried == [LITE_MODELS[0]], "one real request, not a full cascade"
    assert "come back later" in str(exc.value)
    assert "cooling down" in str(exc.value) and LITE_MODELS[1] in str(exc.value)
    assert SECRET not in str(exc.value)
    assert client.attempts == {LITE_MODELS[0]: 2, LITE_MODELS[1]: 1}
    assert client.requests_made == 3


@pytest.mark.asyncio
async def test_a_rejected_key_still_fatal_and_does_not_cool_the_model() -> None:
    clock = [0.0]
    tried: list[str] = []
    status = [401]

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(_model_of(request))
        if status[0] != 200:
            return httpx.Response(status[0], text="denied")
        return httpx.Response(200, json=_ok("ok"))

    client = _clocked(handler, clock)
    with pytest.raises(GeminiAuthError):
        await client.generate("one")
    assert tried == [LITE_MODELS[0]]
    status[0] = 200
    tried.clear()
    await client.generate("two")
    assert tried == [LITE_MODELS[0]], "an auth failure is not a cooldown"


@pytest.mark.asyncio
async def test_the_json_cascade_shares_the_cooldown() -> None:
    clock = [0.0]
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = _model_of(request)
        tried.append(model)
        if model == LITE_MODELS[0]:
            return httpx.Response(429, text="nope")
        return httpx.Response(200, json=_ok('{"name": "W", "count": 1}'))

    client = _clocked(handler, clock)
    await client.generate("one")
    tried.clear()
    assert await client.generate_json("two", _Pick) == _Pick(name="W", count=1)
    assert tried == [LITE_MODELS[1]]


# --- Task 2: the recorder hook -------------------------------------------


@pytest.mark.asyncio
async def test_recorder_is_called_once_per_attempt() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("{}"))

    client = _client(handler, recorder=seen.append)
    assert await client.generate("hi") == "{}"
    assert await client.generate("hi again") == "{}"
    assert seen == [client.models[0], client.models[0]]


@pytest.mark.asyncio
async def test_recorder_counts_each_model_the_cascade_tries() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if _model_of(request) == DEFAULT_MODELS[0]:
            return httpx.Response(429, text="nope")
        return httpx.Response(200, json=_ok("{}"))

    client = _client(handler, recorder=seen.append)
    await client.generate("hi")
    assert seen == [client.models[0], client.models[1]]


@pytest.mark.asyncio
async def test_no_recorder_still_works() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok("{}"))

    client = _client(handler)
    assert await client.generate("hi") == "{}"
