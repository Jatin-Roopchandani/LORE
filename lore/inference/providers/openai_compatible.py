"""A self-hosted model behind OpenAI's chat completions API (local mode only).

llama.cpp's `llama-server`, vLLM, LM Studio and Ollama's `/v1` all serve
`POST {base_url}/chat/completions`; none of them serves the Responses API
the OpenAI provider uses, so this is plain HTTP (httpx), not the SDK.

- Structured output: `response_format = {"type": "json_schema", "json_schema":
  {"name", "schema", "strict": true}}` with the same strict schema Claude and
  OpenAI get (providers/schema.py). Servers differ in how far they honour
  it (a grammar, a hint, or nothing), so EVERY reply is validated against
  the pydantic schema; an invalid one is asked again once, with the reply
  and the validation error in the conversation. A second invalid reply is a
  ProviderOutputError, a GeminiOutputError like every provider's unusable
  reply, so extraction retries then dead-letters the chunk and beats and
  codex skip the item, as ever.
- A server that refuses `json_schema` outright (an HTTP 400 naming it) is
  asked with `{"type": "json_object"}` and the schema in the prompt instead
  (Gemini's JSON_INSTRUCTION), and remembered for the rest of the process.
- A `<think>...</think>` preamble (reasoning models whose server does not
  split it off) and a code fence around the JSON are removed.
- Temperature 0 (merge decisions must not be samples). `max_tokens` from
  `[lore.llm] max_output_tokens` (default 8192; 0 sends none): measured on a
  local 3B, a structured reply can repeat itself until the context is full.
- Usage: the reply's `usage` tokens when the server returns them, reported
  like Claude's and OpenAI's; never a price.
- Concurrency: at most `concurrency` requests in flight per server and event
  loop, shared by every model built for that server (default 1: one GPU).
- The key is optional and only ever sent as a bearer header.
- Refused in multi mode: a server-side URL that users could steer would let
  them reach the server's own network. Self-host mode is the exception: there
  only the server's admin, who owns its network, can set the address.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from lore.inference.providers.base import (
    OPENAI_COMPATIBLE,
    Model,
    ProviderError,
    ProviderKeyError,
    ProviderOutputError,
    ProviderQuotaError,
    ProviderTransportError,
    ProviderUnavailableError,
)
from lore.inference.providers.schema import decode, strict_schema

# A 28B model on a mini PC can take many minutes for one long reply, and a
# queued request waits behind the gate, not inside this timeout.
TIMEOUT_S = 1800.0

MULTI_USER_MESSAGE = (
    'provider = "openai_compatible" is for a local install only and is refused'
    " in multi-user mode (LORE_MULTI_USER): a server-side base URL would let"
    " users reach the server's network.")

RETRY_INSTRUCTION = (
    "Your previous reply was not valid: {error}\n\nReply again with ONLY a JSON"
    " object (no prose, no code fences) matching this JSON Schema:\n{schema}\n")

JSON_INSTRUCTION = (
    "\n\nRespond with ONLY a JSON object (no prose, no code fences) matching "
    "this JSON Schema:\n{schema}\n")

Usage = Callable[[str, str, int, int], None]

_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
# Servers (base URLs) that answered 400 to a json_schema response_format.
_NO_JSON_SCHEMA: set[str] = set()
# One gate per server per event loop (httpx and asyncio primitives are bound
# to the loop that uses them; processing and questions run their own loops).
_GATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" \
    = weakref.WeakKeyDictionary()


class _SchemaRefused(Exception):
    """The server answered 400 to a json_schema response_format."""


class MultiUserRefused(ValueError):
    """The OpenAI-compatible provider was asked for in multi mode."""

    def __init__(self) -> None:
        super().__init__(MULTI_USER_MESSAGE)


def refuse_in_multi_mode() -> None:
    """The hosted multi-user server never builds this provider. Self-host
    mode (LORE_SELFHOST) may: there the only address it is ever given is the
    one the server's admin saved (lore.central.keys; factory.credentials)."""
    from lore.inference import key_scope
    from lore.selfhost import selfhost_enabled
    if key_scope.multi_user_enabled() and not selfhost_enabled():
        raise MultiUserRefused()


def _gate(base_url: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gates = _GATES.setdefault(loop, {})
    if base_url not in gates:
        gates[base_url] = asyncio.Semaphore(max(1, limit))
    return gates[base_url]


def _format_name(schema_name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9_-]", "_", schema_name).strip("_") or "reply")[:64]


def _clean(text: str) -> str:
    """Without a leading think block and a code fence around the reply."""
    text = _THINK.sub("", text, count=1).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _server_says(response: httpx.Response) -> str:
    """The server's own error message (OpenAI's `{"error": {"message"}}`
    shape, or a bare string), else the start of its body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300].strip() or "the request was refused"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:300]
    if isinstance(error, str):
        return error[:300]
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"][:300]
    return response.text[:300].strip() or "the request was refused"


def _refuses_json_schema(response: httpx.Response) -> bool:
    if response.status_code not in (400, 422, 501):
        return False
    text = response.text.lower()
    return any(word in text for word in ("response_format", "json_schema", "schema",
                                         "grammar"))


class OpenAICompatibleTextModel:
    """One model on a self-hosted chat-completions server, as a TextModel."""

    provider = OPENAI_COMPATIBLE

    def __init__(self, api_key: str | None, *, base_url: str, model: str,
                 concurrency: int = 1, max_tokens: int = 8192,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT_S, on_usage: Usage | None = None) -> None:
        refuse_in_multi_mode()
        if not model:
            raise ValueError("an OpenAI-compatible model must be named ([lore.llm] model)")
        self._key = api_key or None
        self._base_url = base_url.rstrip("/")
        self._concurrency = concurrency
        self._max_tokens = max_tokens
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        self._client = httpx.AsyncClient(base_url=self._base_url, headers=headers,
                                         timeout=timeout, transport=transport)
        self.model = model
        self.models = (model,)
        self._on_usage = on_usage
        self.last_model: str | None = None
        self.requests_made = 0
        self.attempts: dict[str, int] = {}
        self.input_tokens = 0
        self.output_tokens = 0

    def __repr__(self) -> str:
        return f"OpenAICompatibleTextModel({self.model!r} at {self._base_url!r})"

    # --- one request ------------------------------------------------------------

    def _error(self, response: httpx.Response) -> BaseException:
        status = response.status_code
        where = f"{self.model} at {self._base_url}"
        if status in (401, 403):
            return ProviderKeyError(
                f"HTTP {status}: the server at {self._base_url} refused the key"
                " (LORE_LLM_API_KEY or ~/.config/lore/llm.env).", provider=OPENAI_COMPATIBLE)
        if status == 429:
            wait = _retry_after(response)
            hint = f" (retry after {wait:g}s)" if wait is not None else ""
            return ProviderQuotaError(f"HTTP 429 from {where}: busy{hint}.",
                                      provider=OPENAI_COMPATIBLE, retry_after=wait)
        if status >= 500:
            return ProviderUnavailableError(
                f"HTTP {status} from {where}: the server is overloaded or failing.",
                provider=OPENAI_COMPATIBLE)
        return ProviderError(f"HTTP {status} from {where}: {_server_says(response)}",
                             provider=OPENAI_COMPATIBLE)

    async def _post(self, messages: list[dict], response_format: dict | None) -> httpx.Response:
        body: dict[str, Any] = {"model": self.model, "messages": messages,
                                "temperature": 0, "stream": False}
        if self._max_tokens:
            body["max_tokens"] = self._max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        self.requests_made += 1
        self.attempts[self.model] = self.attempts.get(self.model, 0) + 1
        key_scope.note_request(self.model)
        started = time.monotonic()
        try:
            async with _gate(self._base_url, self._concurrency):
                response = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            error = ProviderTransportError(
                f"could not reach {self._base_url} ({type(exc).__name__}). Is the"
                " server running?", provider=OPENAI_COMPATIBLE)
            tracing.note_attempt(self.model, ok=False,
                                 latency_s=time.monotonic() - started, error=error)
            raise error from None
        ok = response.status_code < 400
        tracing.note_attempt(self.model, ok=ok, latency_s=time.monotonic() - started,
                             error=None if ok else self._error(response))
        return response

    async def _chat(self, messages: list[dict], response_format: dict | None) -> str:
        response = await self._post(messages, response_format)
        if (response_format is not None and response_format.get("type") == "json_schema"
                and _refuses_json_schema(response)):
            _NO_JSON_SCHEMA.add(self._base_url)
            raise _SchemaRefused()
        if response.status_code >= 400:
            error = self._error(response)
            if isinstance(error, ProviderKeyError):
                key_scope.note_rejection()
            raise error
        try:
            payload = response.json()
            choice = payload["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.last_model = self.model
            raise ProviderOutputError(f"{self.model} sent a reply that is not a chat"
                                      " completion", response.text[:2000], self.model,
                                      provider=OPENAI_COMPATIBLE) from None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            self.input_tokens += tokens_in
            self.output_tokens += tokens_out
            key_scope.note_usage(OPENAI_COMPATIBLE, self.model, tokens_in, tokens_out)
            if self._on_usage is not None:
                self._on_usage(OPENAI_COMPATIBLE, self.model, tokens_in, tokens_out)
        self.last_model = self.model
        if choice.get("finish_reason") == "length":
            raise ProviderOutputError(
                f"{self.model} stopped before finishing (length): the server's context"
                " or output limit is too small for this request", text, self.model,
                provider=OPENAI_COMPATIBLE)
        if not _clean(text):
            raise ProviderOutputError(f"{self.model} returned an empty answer",
                                      text, self.model, provider=OPENAI_COMPATIBLE)
        return text

    # --- TextModel --------------------------------------------------------------

    async def generate(self, prompt: str, *, exhausted_hint: str | None = None) -> str:
        return _clean(await self._chat([{"role": "user", "content": prompt}], None))

    async def generate_json(self, prompt: str, schema: type[Model], *,
                            exhausted_hint: str | None = None,
                            avoid: str | None = None) -> Model:
        strict = strict_schema(schema)
        schema_text = json.dumps(strict)
        messages = [{"role": "user", "content": prompt}]
        fmt: dict | None = None
        if self._base_url not in _NO_JSON_SCHEMA:
            fmt = {"type": "json_schema", "json_schema": {
                "name": _format_name(schema.__name__), "schema": strict, "strict": True}}
            try:
                text = await self._chat(messages, fmt)
            except _SchemaRefused:
                fmt = None
        if fmt is None:
            messages = [{"role": "user",
                         "content": prompt + JSON_INSTRUCTION.format(schema=schema_text)}]
            fmt = {"type": "json_object"}
            text = await self._chat(messages, fmt)
        try:
            return decode(_clean(text), schema)
        except (ValidationError, json.JSONDecodeError) as exc:
            error = _short_error(exc)
        retry = [*messages, {"role": "assistant", "content": text},
                 {"role": "user", "content": RETRY_INSTRUCTION.format(
                     error=error, schema=schema_text)}]
        text = await self._chat(retry, fmt)
        try:
            return decode(_clean(text), schema)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ProviderOutputError(
                f"{self.model} returned output that is not a valid {schema.__name__}"
                f" JSON object, twice ({_short_error(exc)}). Raw start: {text[:200]!r}",
                text, self.model, provider=OPENAI_COMPATIBLE) from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _short_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:5]:
            where = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
            parts.append(f"{where}: {err.get('msg')}")
        return "; ".join(parts)[:600]
    return f"not JSON ({exc})"[:600]


from lore import tracing  # noqa: E402
from lore.inference import key_scope  # noqa: E402
