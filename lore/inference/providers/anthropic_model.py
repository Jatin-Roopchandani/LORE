"""Claude through the official `anthropic` SDK (async client).

- Structured output: `messages.create(output_config={"format": {"type":
  "json_schema", "schema": ...}})`, with the schema made strict in ONE place
  (providers/schema.py), then the first text block parsed and validated.
  Never the deprecated top-level `output_format`, never an assistant prefill
  (a 400 on current models).
- Thinking and effort per model: roles.anthropic_request_options. No
  temperature, top_p or top_k (a 400 since 2026-10-21).
- `stop_reason` is read before any content: `refusal` raises ProviderRefusal
  (a friendly error); `max_tokens` is retried once with a higher cap, then
  reported as an output error.
- Errors are the SDK's typed exceptions, most specific first: auth or
  permission -> ProviderKeyError (key_invalid), rate limit ->
  ProviderQuotaError (the quota path), 529 or 5xx -> ProviderUnavailableError,
  connection -> ProviderTransportError. The SDK has already retried 429, 5xx
  and connection errors (`max_retries`, default 2); nothing here retries them
  again.
- Each response's `usage.input_tokens` / `output_tokens` is reported to the
  bound user's scope (central usage table) and to `on_usage`.

The key lives in the SDK client only; no message here carries it.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from lore.inference.providers import roles
from lore.inference.providers.base import (
    ANTHROPIC,
    Model,
    ProviderError,
    ProviderKeyError,
    ProviderOutputError,
    ProviderQuotaError,
    ProviderRefusal,
    ProviderTransportError,
    ProviderUnavailableError,
)
from lore.inference.providers.schema import decode, strict_schema

MAX_TOKENS = 16000
RETRY_MAX_TOKENS = 32000
TIMEOUT_S = 600.0

Usage = Callable[[str, str, int, int], None]


def _retry_after(exc: Any) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


def map_anthropic_error(exc: BaseException, model: str) -> BaseException | None:
    """The LORE error for an SDK exception, or None if it is not the SDK's."""
    import anthropic

    label = "Claude"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ProviderKeyError(
            f"HTTP {exc.status_code}: {label} rejected the API key.", provider=ANTHROPIC)
    if isinstance(exc, anthropic.RateLimitError):
        wait = _retry_after(exc)
        hint = f" (retry after {wait:g}s)" if wait is not None else ""
        return ProviderQuotaError(f"HTTP 429 from {model}: rate limited{hint}.",
                                  provider=ANTHROPIC, retry_after=wait)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        if status == 529 or status >= 500:
            return ProviderUnavailableError(
                f"HTTP {status} from {model}: {label} is overloaded or failing.",
                provider=ANTHROPIC)
        return ProviderError(f"HTTP {status} from {model}: the request was refused"
                             f" ({type(exc).__name__}).", provider=ANTHROPIC)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderTransportError(
            f"could not reach the Claude API ({type(exc).__name__}).", provider=ANTHROPIC)
    return None


class AnthropicTextModel:
    """One Claude model behind LORE's TextModel surface."""

    provider = ANTHROPIC

    def __init__(self, api_key: str | None, *, model: str = roles.ANTHROPIC_DEFAULT,
                 effort: str = "medium", client: Any = None,
                 max_tokens: int = MAX_TOKENS, retry_max_tokens: int = RETRY_MAX_TOKENS,
                 on_usage: Usage | None = None) -> None:
        if client is None:
            import anthropic
            # An explicit timeout: the SDK then never refuses a large
            # non-streaming max_tokens, and one request can take minutes.
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=TIMEOUT_S)
        self._client = client
        self.model = model
        self.models = (model,)
        self.effort = effort
        self._max_tokens = max_tokens
        self._retry_max_tokens = retry_max_tokens
        self._on_usage = on_usage
        self.last_model: str | None = None
        self.requests_made = 0
        self.attempts: dict[str, int] = {}
        self.input_tokens = 0
        self.output_tokens = 0

    def __repr__(self) -> str:
        return f"AnthropicTextModel({self.model!r}, effort={self.effort!r})"

    async def _request(self, prompt: str, max_tokens: int, schema: dict | None):
        options = roles.anthropic_request_options(self.model, self.effort)
        if schema is not None:
            options["output_config"] = {**options["output_config"],
                                        "format": {"type": "json_schema", "schema": schema}}
        self.requests_made += 1
        self.attempts[self.model] = self.attempts.get(self.model, 0) + 1
        key_scope.note_request(self.model)
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}], **options)
        except Exception as exc:
            mapped = map_anthropic_error(exc, self.model)
            if isinstance(mapped, ProviderKeyError):
                key_scope.note_rejection()
            tracing.note_attempt(self.model, ok=False, latency_s=time.monotonic() - started,
                                 error=mapped or exc)
            if mapped is None:
                raise
            raise mapped from None
        tracing.note_attempt(self.model, ok=True, latency_s=time.monotonic() - started)
        usage = getattr(response, "usage", None)
        if usage is not None:
            tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            key_scope.note_usage(ANTHROPIC, self.model, tokens_in, tokens_out)
            if self._on_usage is not None:
                self._on_usage(ANTHROPIC, self.model, tokens_in, tokens_out)
        return response

    async def _text(self, prompt: str, schema: dict | None) -> str:
        caps = (self._max_tokens, self._retry_max_tokens)
        for attempt, cap in enumerate(caps):
            response = await self._request(prompt, cap, schema)
            reason = getattr(response, "stop_reason", None)
            if reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                self.last_model = self.model
                raise ProviderRefusal(
                    f"{self.model} declined this request"
                    + (f" (category: {category})" if category else "") + ".",
                    provider=ANTHROPIC, model=self.model, category=category)
            if reason == "max_tokens":
                if attempt + 1 < len(caps):
                    continue  # once more, with room to finish
                self.last_model = self.model
                raise ProviderOutputError(
                    f"{self.model} ran out of output tokens ({cap}) before finishing.",
                    "", self.model, provider=ANTHROPIC)
            text = "".join(getattr(block, "text", "") or ""
                           for block in (getattr(response, "content", None) or [])
                           if getattr(block, "type", None) == "text")
            self.last_model = self.model
            if not text.strip():
                raise ProviderOutputError(f"{self.model} returned an empty answer",
                                          text, self.model, provider=ANTHROPIC)
            return text
        raise AssertionError("unreachable")

    async def generate(self, prompt: str, *, exhausted_hint: str | None = None) -> str:
        return await self._text(prompt, None)

    async def generate_json(self, prompt: str, schema: type[Model], *,
                            exhausted_hint: str | None = None,
                            avoid: str | None = None) -> Model:
        text = await self._text(prompt, strict_schema(schema))
        return _decoded(text, schema, self.model, ANTHROPIC)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _decoded(text: str, schema: type[BaseModel], model: str, provider: str):
    try:
        return decode(text, schema)
    except (ValidationError, json.JSONDecodeError) as exc:
        count = exc.error_count() if isinstance(exc, ValidationError) else 1
        raise ProviderOutputError(
            f"{model} returned output that is not a valid {schema.__name__} JSON"
            f" object ({count} error(s)). Raw start: {text[:200]!r}",
            text, model, provider=provider) from exc


from lore import tracing  # noqa: E402
from lore.inference import key_scope  # noqa: E402
