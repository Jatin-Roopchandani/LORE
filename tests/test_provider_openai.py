"""OpenAI through a faked `openai` SDK client (Responses API): request
shape, refusals, incomplete replies, error mapping, usage and the model
suggestion. No network."""
import asyncio
from types import SimpleNamespace as NS

import httpx2
import openai
import pytest
from pydantic import BaseModel

from lore.inference import key_scope
from lore.inference.gemini import GeminiAuthError, GeminiQuotaError, GeminiTransportError
from lore.inference.key_scope import KeyScope
from lore.inference.providers import roles
from lore.inference.providers.base import (
    ProviderKeyError, ProviderOutputError, ProviderQuotaError, ProviderRefusal,
    ProviderTransportError, ProviderUnavailableError,
)
from lore.inference.providers.openai_model import OpenAITextModel
from lore.inference.providers.schema import strict_schema

KEY = "sk-proj-FAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKE0000"


class Reply(BaseModel):
    answer: str
    note: str | None = None


def response(text="", status="completed", reason=None, refusal=None, tokens=(9, 4)):
    content = [NS(type="refusal", refusal=refusal)] if refusal is not None else \
        [NS(type="output_text", text=text)]
    return NS(output=[NS(type="message", content=content)], output_text=text,
              status=status, incomplete_details=NS(reason=reason) if reason else None,
              usage=NS(input_tokens=tokens[0], output_tokens=tokens[1]))


class FakeResponses:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class FakeClient:
    def __init__(self, *replies):
        self.responses = FakeResponses(replies)

    async def close(self):
        pass


def run(coro):
    return asyncio.run(coro)


def _resp(status):
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.openai.com"))


def test_json_request_shape_and_round_trip():
    client = FakeClient(response('{"answer": "Holmes", "note": null}'))
    model = OpenAITextModel(KEY, model="gpt-9-mini", client=client)
    assert run(model.generate_json("Who?", Reply)) == Reply(answer="Holmes")
    call = client.responses.calls[0]
    assert call == {"model": "gpt-9-mini", "input": "Who?", "max_output_tokens": 16000,
                    "store": False,
                    "text": {"format": {"type": "json_schema", "name": "Reply",
                                        "schema": strict_schema(Reply), "strict": True}}}
    assert model.last_model == "gpt-9-mini"


def test_plain_text_has_no_format():
    client = FakeClient(response("yes"))
    assert run(OpenAITextModel(KEY, model="m", client=client).generate("q")) == "yes"
    assert "text" not in client.responses.calls[0]


def test_a_model_must_be_chosen():
    with pytest.raises(ValueError):
        OpenAITextModel(KEY, model="", client=FakeClient())


def test_refusal_and_content_filter_are_refusals():
    for reply in (response(refusal="I can't help with that."),
                  response(status="incomplete", reason="content_filter")):
        with pytest.raises(ProviderRefusal):
            run(OpenAITextModel(KEY, model="m", client=FakeClient(reply)).generate("q"))


def test_incomplete_is_retried_once_then_reported():
    client = FakeClient(response(status="incomplete", reason="max_output_tokens"),
                        response('{"answer": "a"}'))
    model = OpenAITextModel(KEY, model="m", client=client, max_tokens=10, retry_max_tokens=20)
    assert run(model.generate_json("q", Reply)).answer == "a"
    assert [c["max_output_tokens"] for c in client.responses.calls] == [10, 20]
    twice = FakeClient(response(status="incomplete", reason="max_output_tokens"),
                       response(status="incomplete", reason="max_output_tokens"))
    with pytest.raises(ProviderOutputError):
        run(OpenAITextModel(KEY, model="m", client=twice).generate("q"))


@pytest.mark.parametrize("exc,lore_error,base", [
    (openai.AuthenticationError("bad", response=_resp(401), body=None),
     ProviderKeyError, GeminiAuthError),
    (openai.PermissionDeniedError("no", response=_resp(403), body=None),
     ProviderKeyError, GeminiAuthError),
    (openai.RateLimitError("slow", response=_resp(429), body=None),
     ProviderQuotaError, GeminiQuotaError),
    (openai.InternalServerError("boom", response=_resp(503), body=None),
     ProviderUnavailableError, GeminiQuotaError),
    (openai.APIConnectionError(request=httpx2.Request("POST", "https://x")),
     ProviderTransportError, GeminiTransportError),
])
def test_errors_map_to_lore_errors(exc, lore_error, base):
    client = FakeClient(exc)
    with pytest.raises(lore_error) as caught:
        run(OpenAITextModel(KEY, model="m", client=client).generate("q"))
    assert isinstance(caught.value, base) and KEY not in str(caught.value)
    assert len(client.responses.calls) == 1


def test_scope_sees_rejection_requests_and_usage():
    requests, usage = [], []
    scope = KeyScope("bob", KEY, on_request=requests.append, provider="openai",
                     on_usage=lambda *a: usage.append(a))
    client = FakeClient(response("ok", tokens=(50, 7)),
                        openai.AuthenticationError("bad", response=_resp(401), body=None))
    model = OpenAITextModel(KEY, model="gpt-x", client=client)
    with key_scope.bound(scope):
        run(model.generate("q"))
        with pytest.raises(ProviderKeyError):
            run(model.generate("q"))
    assert scope.rejected and requests == ["gpt-x", "gpt-x"]
    assert usage == [("openai", "gpt-x", 50, 7)]


def test_chat_models_and_the_cheapest_suggestion():
    ids = ["gpt-9", "gpt-9-mini", "gpt-9-nano", "gpt-9-nano-2026-01-01", "gpt-10-nano",
           "text-embedding-4", "tts-2", "gpt-9-realtime", "whisper-3", "o7-mini",
           "dall-e-4", "gpt-image-2", "omni-moderation-latest", "babbage-002"]
    chat = roles.openai_chat_models(ids)
    assert "text-embedding-4" not in chat and "gpt-9-realtime" not in chat
    assert "omni-moderation-latest" not in chat and "babbage-002" not in chat
    assert roles.suggest_openai_model(ids) == "gpt-10-nano"
    assert roles.suggest_openai_model(["gpt-9", "o7-mini", "gpt-9-mini"]) == "o7-mini"
    assert roles.suggest_openai_model(["text-embedding-4"]) is None
