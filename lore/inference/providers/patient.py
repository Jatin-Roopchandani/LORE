"""A Claude, OpenAI or self-hosted model that rides out short trouble, for
book processing only (never for answering a question).

The SDKs already retry a 429, a 5xx and a dropped connection a couple of
times within seconds. A processing run lasts hours, so it can afford more:
a rate limit with a `retry-after` (a per-minute limit) is waited out, up to
`patience` seconds per request, and a transport failure or an overload is
retried with the same backoff Gemini's patient client uses. What is left
reaches the caller as before (a short GeminiQuotaError, `daily` False, or a
GeminiTransportError), and processing pauses briefly instead of failing.
Gemini's own cascade does this inside GeminiClient (`patience`).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from lore.inference.gemini import (
    COOLDOWN_SECONDS,
    PROCESSING_PATIENCE,
    TRANSPORT_BACKOFF,
)
from lore.inference.providers.base import (
    ProviderQuotaError,
    ProviderTransportError,
    ProviderUnavailableError,
)

T = TypeVar("T")


class PatientModel:
    """Wraps one TextModel; every other attribute is the wrapped model's."""

    def __init__(self, inner: Any, *, patience: float = PROCESSING_PATIENCE,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 backoff: tuple[float, ...] = TRANSPORT_BACKOFF) -> None:
        self._inner = inner
        self._patience = patience
        self._sleep = sleep
        self._backoff = backoff

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"PatientModel({self._inner!r})"

    async def _call(self, call: Callable[[], Awaitable[T]]) -> T:
        waited, failures = 0.0, 0
        while True:
            try:
                return await call()
            except (ProviderTransportError, ProviderUnavailableError):
                if failures >= len(self._backoff):
                    raise
                await self._sleep(self._backoff[failures])
                failures += 1
            except ProviderQuotaError as exc:
                if exc.daily:
                    raise
                wait = max(1.0, exc.retry_after if exc.retry_after is not None
                           else COOLDOWN_SECONDS)
                if waited + wait > self._patience:
                    raise
                await self._sleep(wait)
                waited += wait

    async def generate(self, prompt: str, *, exhausted_hint: str | None = None) -> str:
        return await self._call(lambda: self._inner.generate(
            prompt, exhausted_hint=exhausted_hint))

    async def generate_json(self, prompt: str, schema, *,
                            exhausted_hint: str | None = None,
                            avoid: str | None = None):
        return await self._call(lambda: self._inner.generate_json(
            prompt, schema, exhausted_hint=exhausted_hint, avoid=avoid))

    async def aclose(self) -> None:
        await self._inner.aclose()
