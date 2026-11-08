"""OpenAI through the official `openai` SDK (async client), Responses API.

- Structured output: `responses.create(text={"format": {"type": "json_schema",
  "name": ..., "schema": ..., "strict": True}})`, the format the installed
  SDK documents (ResponseFormatTextJSONSchemaConfigParam), with the SAME strict
  schema Claude gets (providers/schema.py), then `output_text` parsed and
  validated. `store=False`: no response is kept on OpenAI's side for LORE.
- No temperature and no reasoning effort: the model is whatever the user's
  key offers, and not every model accepts either, so LORE sends neither and
  each model runs at its own defaults.
- A refusal item becomes ProviderRefusal; `incomplete` for
  `max_output_tokens` is retried once with a higher cap, then reported;
  `content_filter` is a refusal.
- Errors: the SDK's typed exceptions, mapped like Claude's (auth -> key,
  429 -> quota, 5xx -> unavailable, connection -> transient). The SDK has
  already retried 429/5xx/connection errors (`max_retries`).
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from lore.inference.providers.anthropic_model import _decoded, _retry_after
from lore.inference.providers.base import (
    OPENAI,
    Model,
    ProviderError,
    ProviderKeyError,
    ProviderOutputError,
    ProviderQuotaError,
    ProviderRefusal,
    ProviderTransportError,
    ProviderUnavailableError,
)
from lore.inference.providers.schema import strict_schema

MAX_TOKENS = 16000
RETRY_MAX_TOKENS = 32000
TIMEOUT_S = 600.0

Usage = Callable[[str, str, int, int], None]


def map_openai_error(exc: BaseException, model: str) -> BaseException | None:
    import openai

    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderKeyError(f"HTTP {exc.status_code}: OpenAI rejected the API key.",
                                provider=OPENAI)
    if isinstance(exc, openai.RateLimitError):
        wait = _retry_after(exc)
        hint = f" (retry after {wait:g}s)" if wait is not None else ""
        return ProviderQuotaError(f"HTTP 429 from {model}: rate limited or out of"
                                  f" credit{hint}.", provider=OPENAI, retry_after=wait)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status >= 500:
            return ProviderUnavailableError(
                f"HTTP {status} from {model}: OpenAI is overloaded or failing.",
                provider=OPENAI)
        return ProviderError(f"HTTP {status} from {model}: the request was refused"
                             f" ({type(exc).__name__}).", provider=OPENAI)
    if isinstance(exc, openai.APIConnectionError):
        return ProviderTransportError(
            f"could not reach the OpenAI API ({type(exc).__name__}).", provider=OPENAI)
    return None


def _format_name(schema_name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]", "_", schema_name).strip("_") or "reply")[:64]


class OpenAITextModel:
    """One OpenAI model behind LORE's TextModel surface."""

    provider = OPENAI

    def __init__(self, api_key: str | None, *, model: str, client: Any = None,
                 max_tokens: int = MAX_TOKENS, retry_max_tokens: int = RETRY_MAX_TOKENS,
                 on_usage: Usage | None = None) -> None:
        if not model:
            raise ValueError("an OpenAI model must be chosen (none is assumed)")
        if client is None:
            import openai
            client = openai.AsyncOpenAI(api_key=api_key, timeout=TIMEOUT_S)
        self._client = client
        self.model = model
        self.models = (model,)
        self._max_tokens = max_tokens
        self._retry_max_tokens = retry_max_tokens
        self._on_usage = on_usage
        self.last_model: str | None = None
        self.requests_made = 0
        self.attempts: dict[str, int] = {}
        self.input_tokens = 0
        self.output_tokens = 0

    def __repr__(self) -> str:
        return f"OpenAITextModel({self.model!r})"

    async def _request(self, prompt: str, max_tokens: int, fmt: dict | None):
        kwargs: dict = {"model": self.model, "input": prompt,
                        "max_output_tokens": max_tokens, "store": False}
        if fmt is not None:
            kwargs["text"] = {"format": fmt}
        self.requests_made += 1
        self.attempts[self.model] = self.attempts.get(self.model, 0) + 1
        key_scope.note_request(self.model)
        started = time.monotonic()
        try:
            response = await self._client.responses.create(**kwargs)
        except Exception as exc:
            mapped = map_openai_error(exc, self.model)
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
            key_scope.note_usage(OPENAI, self.model, tokens_in, tokens_out)
            if self._on_usage is not None:
                self._on_usage(OPENAI, self.model, tokens_in, tokens_out)
        return response

    def _refusal(self, text: str | None) -> ProviderRefusal:
        self.last_model = self.model
        return ProviderRefusal(f"{self.model} declined this request.",
                               provider=OPENAI, model=self.model,
                               category=None if text is None else "refusal")

    async def _text(self, prompt: str, fmt: dict | None) -> str:
        caps = (self._max_tokens, self._retry_max_tokens)
        for attempt, cap in enumerate(caps):
            response = await self._request(prompt, cap, fmt)
            for item in getattr(response, "output", None) or []:
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "refusal":
                        raise self._refusal(getattr(part, "refusal", ""))
            if getattr(response, "status", None) == "incomplete":
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", None)
                if reason == "content_filter":
                    raise self._refusal(None)
                if reason == "max_output_tokens" and attempt + 1 < len(caps):
                    continue
                self.last_model = self.model
                raise ProviderOutputError(
                    f"{self.model} stopped before finishing ({reason or 'incomplete'}).",
                    "", self.model, provider=OPENAI)
            text = getattr(response, "output_text", "") or ""
            self.last_model = self.model
            if not text.strip():
                raise ProviderOutputError(f"{self.model} returned an empty answer",
                                          text, self.model, provider=OPENAI)
            return text
        raise AssertionError("unreachable")

    async def generate(self, prompt: str, *, exhausted_hint: str | None = None) -> str:
        return await self._text(prompt, None)

    async def generate_json(self, prompt: str, schema: type[Model], *,
                            exhausted_hint: str | None = None,
                            avoid: str | None = None) -> Model:
        fmt = {"type": "json_schema", "name": _format_name(schema.__name__),
               "schema": strict_schema(schema), "strict": True}
        text = await self._text(prompt, fmt)
        return _decoded(text, schema, self.model, OPENAI)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


from lore import tracing  # noqa: E402
from lore.inference import key_scope  # noqa: E402
