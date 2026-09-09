"""Gemini free-tier provider — plain REST over the httpx the project owns.

Deliberately NOT an SDK. Google ships two generations of Python client
(`google-generativeai` and `google-genai`) with different surfaces and
different auth plumbing, and picking the wrong one is a silent 401 rather
than an import error. The documented REST endpoint has exactly one shape,
the project already depends on httpx, and this adds no dependency.

Auth is the `x-goog-api-key` header. A Bearer token returns 401 — verified
against the live endpoint, so do not "modernize" it.

The key is read from the environment (`GEMINI_API_KEY`, then
`GOOGLE_API_KEY`) and never from configuration. It is held only in the
request header: nothing here logs it, writes it, or puts it in an
exception message.

MODEL ORDER IS MEASURED, NOT CHRONOLOGICAL. Do not "helpfully" reorder it
by version number. Against a 12-case eval built from the real corpus (6
merges that should happen, 6 that must not, one of which is unresolvable
and must answer "none"):

    gemini-3.1-flash-lite   12/12   15 RPM   500 RPD
    gemini-3.5-flash-lite   11/12   15 RPM   500 RPD   (its one miss was
                                                        prose instead of
                                                        "none")
    gemini-3.8-flash          ---    5 RPM    20 RPD
    gemini-3.6-flash          ---    5 RPM    20 RPD
    gemini-3.7-flash          ---    5 RPM    20 RPD   (stable; 3.8 was
                                                        correct but threw
                                                        repeated 503s)

The OLDER Lite beat the newer one and the Lite tier has 25x the daily
quota of Flash, so the cascade leads with Lite. `gemini-2.5-flash` and
`gemini-2.5-flash-lite` return 404 on this key and must not be listed.

CASCADE, NOT BACKOFF. A 429 (out of budget), a 503 (overloaded) or a 404
(not available to this key) moves to the NEXT MODEL rather than sleeping:
that is what turns five per-model daily quotas into aggregate capacity of
roughly 1100 requests/day for a workload that needs one. 503s were
observed moving between models minute to minute, so a 503 means "try the
next one", never "this model is broken".
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx

ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)


@dataclass(frozen=True)
class ModelLimits:
    """Free-tier ceilings, read off the user's console."""

    rpm: int
    rpd: int


# Ordered by measured score and quota. See the module docstring.
DEFAULT_MODELS: tuple[str, ...] = (
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
)
MODEL_LIMITS: dict[str, ModelLimits] = {
    "gemini-3.1-flash-lite": ModelLimits(rpm=15, rpd=500),
    "gemini-3.5-flash-lite": ModelLimits(rpm=15, rpd=500),
    "gemini-3.8-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3.6-flash": ModelLimits(rpm=5, rpd=20),
    "gemini-3.7-flash": ModelLimits(rpm=5, rpd=20),
}
# An unlisted model id is assumed to be on the stingier Flash tier: guess
# low, so an unknown model never talks us into exceeding a real ceiling.
FALLBACK_LIMITS = ModelLimits(rpm=5, rpd=20)

_KEY_VARIABLES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

# Statuses that mean "this model cannot serve this request" rather than
# "the request is wrong". Every one of them is a reason to try the next
# model in the cascade.
_CASCADE_STATUSES = {
    404: "the model id is not available to this key",
    429: "out of free-tier budget",
    503: "the model is overloaded",
}


class GeminiError(RuntimeError):
    """A request failed. The message never carries the API key."""


class GeminiQuotaError(GeminiError):
    """No model in the cascade could serve the request.

    Also raised internally for one model's 429/503/404 so the cascade can
    catch it and move on. The caller must not write a partial answer set
    when this escapes: a half-written cache would make the next run ask
    the wrong questions.
    """


def limits_for(model: str) -> ModelLimits:
    return MODEL_LIMITS.get(model, FALLBACK_LIMITS)


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The key from the environment, or None. Never from a config file."""
    source = os.environ if env is None else env
    for name in _KEY_VARIABLES:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return None


class GeminiClient:
    """A cascade of free-tier models behind one `generate` call.

    Each model is throttled at its own RPM. A model that says 429/503/404
    is skipped for this request and the next one is tried immediately —
    sleeping through a per-model daily quota would waste the aggregate
    capacity the cascade exists to provide.
    """

    def __init__(
        self,
        api_key: str,
        *,
        models: Sequence[str] = DEFAULT_MODELS,
        endpoint: str = ENDPOINT,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not models:
            raise ValueError("at least one model id is required")
        self._api_key = api_key
        self.models = tuple(models)
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._sleep = sleep
        self._now = now
        self._last_request_at: dict[str, float] = {}
        self.requests_made = 0
        # Per-model attempt counts, so a caller can report honestly which
        # daily budget it actually spent. A cascade that fell through two
        # models has spent one request against each of them, not three
        # against the model that finally answered.
        self.attempts: dict[str, int] = {}
        self.last_model: str | None = None

    @property
    def daily_limit(self) -> int:
        """The daily budget of the model that answered, or of the first."""
        return limits_for(self.last_model or self.models[0]).rpd

    async def _throttle(self, model: str) -> None:
        last = self._last_request_at.get(model)
        if last is None:
            return
        remaining = (60.0 / max(limits_for(model).rpm, 1)) - (self._now() - last)
        if remaining > 0:
            await self._sleep(remaining)

    async def _generate_one(self, model: str, prompt: str) -> str:
        await self._throttle(model)
        self._last_request_at[model] = self._now()
        self.requests_made += 1
        self.attempts[model] = self.attempts.get(model, 0) + 1
        url = self._endpoint.format(model=model)
        try:
            response = await self._client.post(
                url,
                headers={"x-goog-api-key": self._api_key,
                         "content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    # A merge decision must be a function of its inputs, not
                    # a sample: the same corpus has to produce the same
                    # graph. Same reason the Ollama adjudicator pins it.
                    "generationConfig": {"temperature": 0},
                },
            )
        except httpx.HTTPError as exc:
            raise GeminiError(f"could not reach the Gemini API: {exc}") from exc

        if response.status_code in (401, 403):
            # Fatal, and never cascaded: a rejected key will be rejected by
            # every model, so trying four more only wastes time.
            raise GeminiError(
                f"HTTP {response.status_code}: the API key was rejected. Auth"
                " is the `x-goog-api-key` header, not a Bearer token — check"
                f" {' or '.join(_KEY_VARIABLES)} in your environment."
            )
        if response.status_code in _CASCADE_STATUSES:
            limits = limits_for(model)
            raise GeminiQuotaError(
                f"HTTP {response.status_code} from {model}"
                f" ({_CASCADE_STATUSES[response.status_code]};"
                f" {limits.rpm} RPM, {limits.rpd} requests/day)"
            )
        if response.status_code >= 400:
            raise GeminiError(
                f"HTTP {response.status_code} from {model}:"
                f" {response.text[:400]}"
            )

        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiError(
                f"{model} returned no candidates"
                f" (promptFeedback={payload.get('promptFeedback')})"
            )
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text") or "") for part in parts)
        if not text.strip():
            raise GeminiError(f"{model} returned an empty answer")
        return text

    async def generate(self, prompt: str) -> str:
        """One completion at temperature 0, from the first willing model."""
        refusals: list[str] = []
        for model in self.models:
            try:
                text = await self._generate_one(model, prompt)
            except GeminiQuotaError as exc:
                refusals.append(str(exc))
                continue
            self.last_model = model
            return text
        raise GeminiQuotaError(
            "every configured model refused the request — "
            + "; ".join(refusals)
            + ". Nothing was written to the answer cache; re-run"
            " `lore disambiguate` when a daily budget resets."
        )

    async def aclose(self) -> None:
        await self._client.aclose()
