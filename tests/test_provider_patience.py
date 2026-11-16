"""PatientModel: a Claude/OpenAI model built for processing waits out a
short 429 (its retry-after) and retries transport failures; a daily one, or
patience running out, reaches the caller. No network: a scripted model."""
import pytest

from lore.inference.gemini import TRANSPORT_BACKOFF
from lore.inference.providers.base import (
    ProviderQuotaError,
    ProviderTransportError,
    ProviderUnavailableError,
)
from lore.inference.providers.patient import PatientModel


class Scripted:
    models = ("m",)
    last_model = "m"

    def __init__(self, *outcomes):
        self.outcomes, self.calls, self.closed = list(outcomes), 0, False

    async def generate(self, prompt, *, exhausted_hint=None):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    async def generate_json(self, prompt, schema, *, exhausted_hint=None, avoid=None):
        return await self.generate(prompt)

    async def aclose(self):
        self.closed = True


def _patient(inner, **kwargs):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    return PatientModel(inner, sleep=sleep, **kwargs), slept


def _limited(after=30.0, daily=False):
    return ProviderQuotaError("HTTP 429", provider="anthropic", retry_after=after, daily=daily)


@pytest.mark.asyncio
async def test_a_short_429_is_waited_out_for_its_retry_after() -> None:
    inner = Scripted(_limited(30.0), "ok")
    model, slept = _patient(inner)
    assert await model.generate("x") == "ok"
    assert slept == [30.0] and inner.calls == 2
    assert model.models == ("m",) and model.last_model == "m"  # delegated


@pytest.mark.asyncio
async def test_a_daily_429_is_raised_at_once() -> None:
    model, slept = _patient(Scripted(_limited(daily=True)))
    with pytest.raises(ProviderQuotaError):
        await model.generate_json("x", dict)
    assert slept == []


@pytest.mark.asyncio
async def test_short_429s_past_patience_are_raised() -> None:
    model, slept = _patient(Scripted(*[_limited(60.0)] * 5), patience=100.0)
    with pytest.raises(ProviderQuotaError):
        await model.generate("x")
    assert slept == [60.0]


@pytest.mark.asyncio
async def test_transport_failures_and_overloads_are_retried_with_backoff() -> None:
    errors = [ProviderTransportError("down", provider="openai"),
              ProviderUnavailableError("HTTP 529", provider="anthropic")]
    model, slept = _patient(Scripted(*errors, "ok"))
    assert await model.generate("x") == "ok"
    assert slept == list(TRANSPORT_BACKOFF[:2])


@pytest.mark.asyncio
async def test_a_transport_failure_that_persists_is_raised() -> None:
    down = ProviderTransportError("down", provider="openai")
    inner = Scripted(*[down] * (len(TRANSPORT_BACKOFF) + 1))
    model, slept = _patient(inner)
    with pytest.raises(ProviderTransportError):
        await model.generate("x")
    assert slept == list(TRANSPORT_BACKOFF)
    await model.aclose()
    assert inner.closed


def test_openai_insufficient_quota_is_daily_and_a_rate_limit_is_not() -> None:
    from lore.inference.providers.openai_model import map_openai_error
    import httpx
    import openai

    def error(code):
        response = httpx.Response(429, headers={"retry-after": "7"},
                                  request=httpx.Request("POST", "https://api.openai.com"))
        return openai.RateLimitError("limited", response=response,
                                     body={"code": code})  # as the SDK unwraps it

    rate = map_openai_error(error("rate_limit_exceeded"), "gpt")
    broke = map_openai_error(error("insufficient_quota"), "gpt")
    assert (rate.daily, rate.retry_after) == (False, 7.0)
    assert broke.daily is True
