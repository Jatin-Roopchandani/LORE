"""Claude through a faked `anthropic` SDK client: request shape, stop
reasons, error mapping and usage. No network."""
import asyncio
import json
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from lore.inference import key_scope
from lore.inference.gemini import (
    GeminiAuthError, GeminiOutputError, GeminiQuotaError, GeminiTransportError,
)
from lore.inference.key_scope import KeyScope
from lore.inference.providers import roles
from lore.inference.providers.anthropic_model import AnthropicTextModel
from lore.inference.providers.base import (
    ProviderKeyError, ProviderOutputError, ProviderQuotaError, ProviderRefusal,
    ProviderTransportError, ProviderUnavailableError,
)
from lore.inference.providers.schema import strict_schema

KEY = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfakeFAKEfake-0000"


class Reply(BaseModel):
    answer: str
    tags: list[str] = []


def message(text="", stop="end_turn", tokens=(10, 5), details=None, blocks=None):
    content = blocks if blocks is not None else [NS(type="text", text=text)]
    return NS(content=content, stop_reason=stop, stop_details=details,
              usage=NS(input_tokens=tokens[0], output_tokens=tokens[1]))


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeClient:
    def __init__(self, *replies):
        self.messages = FakeMessages(replies)
        self.closed = False

    async def close(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def _response(status, headers=None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status, request=request, headers=headers or {})


def test_json_request_shape_and_round_trip():
    client = FakeClient(message('{"answer": "Watson", "tags": null}'))
    model = AnthropicTextModel(KEY, model=roles.CLAUDE_SONNET, effort="medium", client=client)
    out = run(model.generate_json("Who narrates?", Reply))
    assert out == Reply(answer="Watson")
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["messages"] == [{"role": "user", "content": "Who narrates?"}]  # no prefill
    assert call["output_config"] == {"effort": "medium", "format": {
        "type": "json_schema", "schema": strict_schema(Reply)}}
    assert "thinking" not in call  # adaptive (the model's default) for writing
    for banned in ("temperature", "top_p", "top_k", "output_format", "system"):
        assert banned not in call
    assert model.last_model == "claude-sonnet-5-5"
    assert (model.input_tokens, model.output_tokens) == (10, 5)


@pytest.mark.parametrize("model_id,effort,thinking", [
    (roles.CLAUDE_SONNET, "low", {"type": "between_tools"}),
    (roles.CLAUDE_SONNET, "medium", None),
    (roles.CLAUDE_OPUS, "low", None),
    (roles.CLAUDE_OPUS, "medium", None),
    (roles.CLAUDE_HAIKU, "low", None),
])
def test_thinking_and_effort_per_model(model_id, effort, thinking):
    client = FakeClient(message("yes"))
    run(AnthropicTextModel(KEY, model=model_id, effort=effort, client=client).generate("q"))
    call = client.messages.calls[0]
    assert call["output_config"] == {"effort": effort}  # always explicit
    assert call.get("thinking") == thinking
    assert call.get("thinking") != {"type": "disabled"}


def test_plain_text_joins_text_blocks_and_skips_thinking():
    blocks = [NS(type="thinking", thinking=""), NS(type="text", text="ye"),
              NS(type="text", text="s")]
    client = FakeClient(message(blocks=blocks))
    assert run(AnthropicTextModel(KEY, client=client).generate("q")) == "yes"
    assert "output_config" in client.messages.calls[0]
    assert "format" not in client.messages.calls[0]["output_config"]


def test_refusal_is_a_friendly_error_not_a_crash():
    details = NS(type="refusal", category="cyber", explanation="...")
    client = FakeClient(message("", stop="refusal", details=details))
    with pytest.raises(ProviderRefusal) as caught:
        run(AnthropicTextModel(KEY, client=client).generate_json("q", Reply))
    assert caught.value.category == "cyber"
    assert isinstance(caught.value, GeminiOutputError)  # extraction skips the passage
    assert len(client.messages.calls) == 1


def test_max_tokens_is_retried_once_with_a_higher_cap():
    client = FakeClient(message('{"answer": "cut', stop="max_tokens"),
                        message('{"answer": "whole"}'))
    model = AnthropicTextModel(KEY, client=client, max_tokens=100, retry_max_tokens=400)
    assert run(model.generate_json("q", Reply)).answer == "whole"
    assert [c["max_tokens"] for c in client.messages.calls] == [100, 400]


def test_max_tokens_twice_is_reported():
    client = FakeClient(message("a", stop="max_tokens"), message("a", stop="max_tokens"))
    with pytest.raises(ProviderOutputError, match="ran out of output tokens"):
        run(AnthropicTextModel(KEY, client=client).generate("q"))


def test_off_schema_output_is_an_output_error():
    client = FakeClient(message('{"wrong": 1}'))
    with pytest.raises(ProviderOutputError) as caught:
        run(AnthropicTextModel(KEY, client=client).generate_json("q", Reply))
    assert caught.value.raw_output == '{"wrong": 1}'


def test_empty_answer_is_an_output_error():
    with pytest.raises(ProviderOutputError):
        run(AnthropicTextModel(KEY, client=FakeClient(message("  "))).generate("q"))


@pytest.mark.parametrize("exc,lore_error,base", [
    (anthropic.AuthenticationError("bad", response=_response(401), body=None),
     ProviderKeyError, GeminiAuthError),
    (anthropic.PermissionDeniedError("no", response=_response(403), body=None),
     ProviderKeyError, GeminiAuthError),
    (anthropic.RateLimitError("slow", response=_response(429, {"retry-after": "7"}),
                              body=None), ProviderQuotaError, GeminiQuotaError),
    (anthropic.InternalServerError("boom", response=_response(500), body=None),
     ProviderUnavailableError, GeminiQuotaError),
    (anthropic.APIStatusError("overloaded", response=_response(529), body=None),
     ProviderUnavailableError, GeminiQuotaError),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
     ProviderTransportError, GeminiTransportError),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")),
     ProviderTransportError, GeminiTransportError),
])
def test_errors_map_to_lore_errors(exc, lore_error, base):
    client = FakeClient(exc)
    with pytest.raises(lore_error) as caught:
        run(AnthropicTextModel(KEY, client=client).generate("q"))
    assert isinstance(caught.value, base)
    assert KEY not in str(caught.value)
    assert len(client.messages.calls) == 1  # the SDK already retried; we do not


def test_rate_limit_carries_retry_after():
    exc = anthropic.RateLimitError("slow", response=_response(429, {"retry-after": "7"}),
                                   body=None)
    with pytest.raises(ProviderQuotaError) as caught:
        run(AnthropicTextModel(KEY, client=FakeClient(exc)).generate("q"))
    assert caught.value.retry_after == 7


def test_bad_request_is_a_plain_provider_error():
    exc = anthropic.BadRequestError("bad", response=_response(400), body=None)
    with pytest.raises(Exception) as caught:
        run(AnthropicTextModel(KEY, client=FakeClient(exc)).generate("q"))
    assert not isinstance(caught.value, (GeminiQuotaError, GeminiAuthError))


def test_a_refused_key_marks_the_bound_scope_and_usage_is_counted():
    requests, usage = [], []
    scope = KeyScope("alice", KEY, on_request=requests.append, provider="anthropic",
                     on_usage=lambda *a: usage.append(a))
    client = FakeClient(message("ok", tokens=(120, 30)),
                        anthropic.AuthenticationError("bad", response=_response(401),
                                                      body=None))
    model = AnthropicTextModel(KEY, client=client)
    with key_scope.bound(scope):
        run(model.generate("q"))
        assert not scope.rejected
        with pytest.raises(ProviderKeyError):
            run(model.generate("q"))
    assert scope.rejected
    assert requests == ["claude-sonnet-5-5", "claude-sonnet-5-5"]
    assert usage == [("anthropic", "claude-sonnet-5-5", 120, 30)]


def test_on_usage_callback_and_close():
    seen = []
    client = FakeClient(message("ok"))
    model = AnthropicTextModel(KEY, client=client, on_usage=lambda *a: seen.append(a))
    run(model.generate("q"))
    run(model.aclose())
    assert seen == [("anthropic", "claude-sonnet-5-5", 10, 5)] and client.closed


def test_the_real_client_is_built_with_the_key_and_no_request(monkeypatch):
    model = AnthropicTextModel(KEY, model=roles.CLAUDE_HAIKU, effort="low")
    assert isinstance(model._client, anthropic.AsyncAnthropic)
    assert KEY not in repr(model)
    run(model.aclose())


def test_cost_table():
    assert roles.anthropic_cost("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0
    assert roles.anthropic_cost("claude-haiku-5-5", 2_000_000, 0) == pytest.approx(0.2)
    assert roles.anthropic_cost("unknown", 1, 1) is None


def test_json_is_valid_for_every_request():
    client = FakeClient(message('{"answer": "a", "tags": []}'))
    run(AnthropicTextModel(KEY, client=client).generate_json("q", Reply))
    json.dumps(client.messages.calls[0]["output_config"])
