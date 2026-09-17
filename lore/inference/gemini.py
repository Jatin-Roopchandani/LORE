"""Gemini free-tier provider — plain REST over the httpx the project owns.

Deliberately NOT an SDK. Google ships two generations of Python client
(`google-generativeai` and `google-genai`) with different surfaces and
different auth plumbing, and picking the wrong one is a silent 401 rather
than an import error. The documented REST endpoint has exactly one shape,
the project already depends on httpx, and this adds no dependency.

Auth is the `x-goog-api-key` header. A Bearer token returns 401 — verified
against the live endpoint, so do not "modernize" it.

The key is read from the environment (`GEMINI_API_KEY`, then
`GOOGLE_API_KEY`), then from `~/.config/lore/gemini.env` when the caller
passes that file, and never from lore.toml. The file is refused if its mode
lets anyone but its owner read it. The key is held only in the request
header: nothing here logs it, writes it, or puts it in an exception message.

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
import json
import os
import stat
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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
# The Lite tier alone: 500 requests/day each. `lore ask`'s opt-in synthesis
# defaults to these so a question never spends the Flash tier's 20/day,
# which the disambiguator shares.
LITE_MODELS: tuple[str, ...] = DEFAULT_MODELS[:2]
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
_KEY_FILE_VARIABLE = "GEMINI_API_KEY"

# Appended to a structured-generation prompt. Measured in Task 7c: with
# responseMimeType JSON and this tail, Flash Lite returned schema-valid JSON
# on 8 of 8 synthesis prompts.
JSON_INSTRUCTION = (
    "\n\nRespond with ONLY a JSON object (no prose, no code fences) matching "
    "this JSON Schema:\n{schema}\n"
)

Model = TypeVar("Model", bound=BaseModel)

# Statuses that mean "this model cannot serve this request" rather than
# "the request is wrong". Every one of them is a reason to try the next
# model in the cascade.
# How long a model that answered 429/503/404 is skipped by this client.
# Without it, once a model's DAILY quota is gone every call spends a wasted
# request on it before cascading -- measured in Tasks 10d/10e, where each
# answer cost 2 requests. A minute is long enough for a per-minute (RPM)
# refusal or a passing 503 to clear, and short enough that a daily-quota
# refusal costs at most one wasted request a minute (it simply re-cools each
# time it is retried) rather than one per call.
COOLDOWN_SECONDS = 60.0

_CASCADE_STATUSES = {
    404: "the model id is not available to this key",
    429: "out of free-tier budget",
    503: "the model is overloaded",
}


class GeminiError(RuntimeError):
    """A request failed. The message never carries the API key."""


class GeminiKeyFileError(GeminiError):
    """The key file exists but must not be trusted. Never carries the key."""


class GeminiAuthError(GeminiError):
    """HTTP 401/403: the key was rejected. Fatal; never cascaded."""


class GeminiTransportError(GeminiError):
    """The API could not be reached at all (DNS, connect, timeout)."""


class GeminiOutputError(GeminiError):
    """A model answered, but not with a usable answer.

    Empty candidates, blank text, or JSON that fails the schema. This is the
    one Gemini failure that is about the REQUEST's content rather than about
    capacity, so extraction retries it and then dead-letters the chunk. It
    carries the raw text (like `ProviderOutputError`) so the dead letter
    stays diagnosable.
    """

    def __init__(self, message: str, raw_output: str = "") -> None:
        super().__init__(message)
        self.raw_output = raw_output


class GeminiQuotaError(GeminiError):
    """No model in the cascade could serve the request.

    Also raised internally for one model's 429/503/404 so the cascade can
    catch it and move on. The caller must not write a partial answer set
    when this escapes: a half-written cache would make the next run ask
    the wrong questions.
    """


def limits_for(model: str) -> ModelLimits:
    return MODEL_LIMITS.get(model, FALLBACK_LIMITS)


def default_key_file() -> Path:
    """`~/.config/lore/gemini.env`: outside the repo, so never committed."""
    return Path.home() / ".config" / "lore" / "gemini.env"


def _key_from_file(path: Path) -> str | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if mode & ~0o600:
        # Refuse rather than read: a key other accounts can read is a key
        # that may already be theirs, and reading it anyway teaches nobody.
        raise GeminiKeyFileError(
            f"refusing to read {path}: its mode is {oct(mode)[2:]}, which lets"
            f" someone other than its owner read it. Run `chmod 600 {path}`."
        )
    for line in path.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == _KEY_FILE_VARIABLE:
            value = value.strip().strip("'\"").strip()
            if value:
                return value
    return None


def resolve_api_key(
    env: Mapping[str, str] | None = None, *, key_file: Path | None = None
) -> str | None:
    """The key from the environment, then from `key_file`, or None.

    Never from lore.toml. `key_file` is opt-in per caller (normally
    `default_key_file()`), so a caller that passes none reads the
    environment only. Raises GeminiKeyFileError for a file others can read.
    """
    source = os.environ if env is None else env
    for name in _KEY_VARIABLES:
        value = (source.get(name) or "").strip()
        if value:
            return value
    if key_file is not None:
        return _key_from_file(key_file)
    return None


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


class GeminiClient:
    """A cascade of free-tier models behind one `generate` call.

    Each model is throttled at its own RPM. A model that says 429/503/404
    is skipped for this request and the next one is tried immediately —
    sleeping through a per-model daily quota would waste the aggregate
    capacity the cascade exists to provide. The refusing model then cools
    down for COOLDOWN_SECONDS: later calls start from the next model instead
    of re-spending a request on it. If every model is cooling, the one whose
    cooldown ends soonest is tried rather than failing without a request.
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
        # Model -> the clock time its cooldown ends. Set by a 429/503/404.
        self._cooling_until: dict[str, float] = {}

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

    async def _generate_one(
        self, model: str, prompt: str, generation_config: dict
    ) -> str:
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
                    "generationConfig": generation_config,
                },
            )
        except httpx.HTTPError as exc:
            raise GeminiTransportError(
                f"could not reach the Gemini API: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            # Fatal, and never cascaded: a rejected key will be rejected by
            # every model, so trying four more only wastes time.
            raise GeminiAuthError(
                f"HTTP {response.status_code}: the API key was rejected. Auth"
                " is the `x-goog-api-key` header, not a Bearer token — check"
                f" {' or '.join(_KEY_VARIABLES)} in your environment, or"
                " ~/.config/lore/gemini.env."
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
            raise GeminiOutputError(
                f"{model} returned no candidates"
                f" (promptFeedback={payload.get('promptFeedback')})"
            )
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text") or "") for part in parts)
        if not text.strip():
            raise GeminiOutputError(f"{model} returned an empty answer", text)
        return text

    async def _cascade(
        self, prompt: str, generation_config: dict, exhausted_hint: str
    ) -> str:
        refusals: list[str] = []
        willing = self._willing_models()
        for model in willing:
            try:
                text = await self._generate_one(model, prompt, generation_config)
            except GeminiQuotaError as exc:
                refusals.append(str(exc))
                self._cooling_until[model] = self._now() + COOLDOWN_SECONDS
                continue
            self._cooling_until.pop(model, None)
            self.last_model = model
            return text
        skipped = [m for m in self.models if m not in willing]
        cooling = (f" (cooling down after an earlier refusal: {', '.join(skipped)})"
                   if skipped else "")
        raise GeminiQuotaError(
            "every configured model refused the request — "
            + "; ".join(refusals) + cooling + ". " + exhausted_hint
        )

    def _willing_models(self) -> list[str]:
        """The configured models not cooling down, in cascade order.

        When every one is cooling, the one whose cooldown ends soonest
        (first in cascade order on a tie): one real request is better than
        failing without asking, and a daily-quota refusal just re-cools it.
        """
        now = self._now()
        ready = [m for m in self.models if self._cooling_until.get(m, now) <= now]
        if ready:
            return ready
        return [min(self.models, key=lambda m: self._cooling_until[m])]

    async def generate(
        self, prompt: str, *, exhausted_hint: str | None = None
    ) -> str:
        """One completion at temperature 0, from the first willing model.

        `exhausted_hint` ends the message of the GeminiQuotaError raised when
        every model refuses; the default is the disambiguator's advice.
        """
        # A merge decision must be a function of its inputs, not a sample:
        # the same corpus has to produce the same graph. Same reason the
        # Ollama adjudicator pins it.
        return await self._cascade(
            prompt, {"temperature": 0},
            exhausted_hint if exhausted_hint is not None else
            "Nothing was written to the answer cache; re-run"
            " `lore disambiguate` when a daily budget resets.",
        )

    async def generate_json(
        self, prompt: str, schema: type[Model], *, exhausted_hint: str | None = None
    ) -> Model:
        """One schema-valid object at temperature 0, from the first willing model.

        The shape measured in Task 7c: JSON response mime type, the schema
        appended to the prompt, a stray code fence stripped, then strict
        validation. Invalid output is an error, not a retry on the next
        model: the cascade exists for capacity, not for second opinions.
        """
        text = await self._cascade(
            prompt + JSON_INSTRUCTION.format(
                schema=json.dumps(schema.model_json_schema())
            ),
            {"temperature": 0, "responseMimeType": "application/json"},
            exhausted_hint if exhausted_hint is not None else
            "Re-run when a daily budget resets, or set [lore.synthesis]"
            ' provider = "ollama" to answer locally.',
        )
        try:
            return schema.model_validate_json(_strip_fence(text))
        except ValidationError as exc:
            raise GeminiOutputError(
                f"{self.last_model} returned output that is not a valid"
                f" {schema.__name__} JSON object ({exc.error_count()} error(s):"
                f" {str(exc.errors()[0].get('msg'))[:200]}). Raw start:"
                f" {text[:200]!r}",
                text,
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
