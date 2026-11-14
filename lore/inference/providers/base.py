"""What every LORE text model offers, and the errors a provider raises.

`TextModel` is the surface LORE's model calls actually use today (read off
every call site: query models, extraction, adjudication and the narrator,
disambiguation, beats, codex and the entity intro). `GeminiClient` already
has it; the Claude and OpenAI models implement the same one, so no call site
needed to change how it calls a model.

The errors subclass the Gemini ones on purpose. Every caller already sorts
`GeminiAuthError` (the key was refused), `GeminiQuotaError` (no capacity
now), `GeminiTransportError` (unreachable) and `GeminiOutputError` (an
unusable reply, which extraction retries then dead-letters) into the right
outcome; a Claude rate limit must land in the same place as a Gemini 429.
"""
from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from lore.inference.gemini import (
    GeminiAuthError,
    GeminiError,
    GeminiOutputError,
    GeminiQuotaError,
    GeminiTransportError,
)

GEMINI = "gemini"
ANTHROPIC = "anthropic"
OPENAI = "openai"
# What a user may choose (multi mode's Account settings, the central keys).
PROVIDERS = (GEMINI, ANTHROPIC, OPENAI)
# Local mode only, never in PROVIDERS: a self-hosted server that speaks
# OpenAI's chat completions (providers/openai_compatible.py).
OPENAI_COMPATIBLE = "openai_compatible"
LABELS = {GEMINI: "Gemini", ANTHROPIC: "Claude", OPENAI: "OpenAI",
          OPENAI_COMPATIBLE: "Local model"}

Model = TypeVar("Model", bound=BaseModel)


class TextModel(Protocol):
    """One model (or Gemini's cascade of models) behind two calls."""

    models: tuple[str, ...]
    last_model: str | None
    requests_made: int
    attempts: dict[str, int]

    async def generate(self, prompt: str, *, exhausted_hint: str | None = None) -> str:
        """Plain text."""
        ...

    async def generate_json(self, prompt: str, schema: type[Model], *,
                            exhausted_hint: str | None = None,
                            avoid: str | None = None) -> Model:
        """One object valid against `schema`."""
        ...

    async def aclose(self) -> None:
        ...


class ProviderError(GeminiError):
    """A Claude or OpenAI request failed. Never carries the key."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderKeyError(GeminiAuthError):
    """The provider refused the key (401/403): `key_invalid`."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderQuotaError(GeminiQuotaError):
    """Rate limited (429), after the SDK's own retries: the quota path."""

    def __init__(self, message: str, *, provider: str,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


class ProviderUnavailableError(GeminiQuotaError):
    """Overloaded (529) or a server error (5xx), after the SDK's retries.

    A GeminiQuotaError like Gemini's own 503, so it lands where a Gemini
    overload does (`upstream_unavailable`; a job waits and continues).
    """

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderTransportError(GeminiTransportError):
    """The provider could not be reached (DNS, connect, timeout): transient."""

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderOutputError(GeminiOutputError):
    """The model answered, but not usably (cut off, empty, off-schema)."""

    def __init__(self, message: str, raw_output: str = "", model: str = "", *,
                 provider: str) -> None:
        super().__init__(message, raw_output, model)
        self.provider = provider


class ProviderRefusal(ProviderOutputError):
    """The model declined (Claude's `refusal` stop reason, OpenAI's refusal).

    An output error, so extraction skips the passage like any other unusable
    reply; a question shows a friendly `model_refused` message.
    """

    def __init__(self, message: str, *, provider: str, model: str = "",
                 category: str | None = None) -> None:
        super().__init__(message, "", model, provider=provider)
        self.category = category
