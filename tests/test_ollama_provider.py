import json

import httpx
import pytest
from pydantic import BaseModel

from lore.config import OllamaConfig
from lore.extract.schemas import MAX_OUTPUT_TOKENS
from lore.inference.base import ProviderOutputError
from lore.inference.ollama import _TRUNCATION_HINT_CHARS, OllamaProvider


class Answer(BaseModel):
    city: str


def _provider_with(handler) -> OllamaProvider:
    transport = httpx.MockTransport(handler)
    provider = OllamaProvider(OllamaConfig())
    provider._client = httpx.AsyncClient(
        transport=transport, base_url=provider._client.base_url
    )
    return provider


@pytest.mark.asyncio
async def test_generate_parses_structured_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "qwen2.5:3b-instruct-q4_K_M"
        assert body["format"]["properties"]["city"]["type"] == "string"
        assert body["stream"] is False
        return httpx.Response(
            200, json={"message": {"role": "assistant",
                                   "content": '{"city": "Paris"}'}}
        )

    provider = _provider_with(handler)
    result = await provider.generate("Capital of France as JSON.", Answer)
    assert result == Answer(city="Paris")


@pytest.mark.asyncio
async def test_generate_sends_an_explicit_output_cap() -> None:
    # The Kaggle tier caps output; the local tier must cap it the same
    # way or the same book dead-letters differently per provider. An
    # uncapped local run also truncates at the model's own default,
    # which is invisible until the JSON fails to parse.
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200, json={"message": {"role": "assistant",
                                   "content": '{"city": "Paris"}'}}
        )

    provider = _provider_with(handler)
    await provider.generate("Capital of France as JSON.", Answer)
    assert sent["options"]["num_predict"] == MAX_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_generate_merges_caller_options_over_the_default() -> None:
    # Query-time selection needs temperature 0 AND seed 0 to be a function
    # of its inputs (see lore/graph/adjudicate.py); extraction must keep
    # sending exactly what it sent before.
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["options"])
        return httpx.Response(
            200, json={"message": {"role": "assistant",
                                   "content": '{"city": "Paris"}'}}
        )

    provider = _provider_with(handler)
    await provider.generate("Capital of France as JSON.", Answer)
    await provider.generate("Capital of France as JSON.", Answer,
                            options={"temperature": 0, "seed": 0})
    await provider.generate("Capital of France as JSON.", Answer,
                            options={"num_predict": 64})
    assert sent[0] == {"num_predict": MAX_OUTPUT_TOKENS}
    assert sent[1] == {"num_predict": MAX_OUTPUT_TOKENS, "temperature": 0, "seed": 0}
    assert sent[2] == {"num_predict": 64}


@pytest.mark.asyncio
async def test_generate_raises_provider_output_error_on_malformed_response() -> None:
    # FIX 4: a schema-validation failure must carry the raw text so dead
    # letters stay diagnosable, not just the bare exception string.
    raw = '{"city": 42}'  # wrong type for a required field

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": raw}}
        )

    provider = _provider_with(handler)
    with pytest.raises(ProviderOutputError) as exc_info:
        await provider.generate("Capital of France as JSON.", Answer)
    assert exc_info.value.raw_output == raw


@pytest.mark.asyncio
async def test_generate_error_flags_likely_truncation_near_the_cap() -> None:
    # A raw output within ~5% of the MAX_OUTPUT_TOKENS character span is
    # a plausible truncation, not just a malformed model output - this is
    # exactly how the 26 real dead letters looked (6367-8329 chars).
    near_cap = '{"city": "' + "x" * (int(_TRUNCATION_HINT_CHARS) - 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": near_cap}}
        )

    provider = _provider_with(handler)
    with pytest.raises(ProviderOutputError) as exc_info:
        await provider.generate("Capital of France as JSON.", Answer)
    assert "truncat" in str(exc_info.value).lower()
    assert exc_info.value.raw_output == near_cap


@pytest.mark.asyncio
async def test_generate_error_omits_truncation_hint_when_far_below_cap() -> None:
    # A short malformed response should read as malformed, not truncated.
    raw = '{"city": 42}'  # wrong type, tiny compared to the cap's span

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": raw}}
        )

    provider = _provider_with(handler)
    with pytest.raises(ProviderOutputError) as exc_info:
        await provider.generate("Capital of France as JSON.", Answer)
    assert "truncat" not in str(exc_info.value).lower()
    assert exc_info.value.raw_output == raw


@pytest.mark.asyncio
async def test_healthy_false_when_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _provider_with(handler)
    assert await provider.healthy() is False


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_roundtrip() -> None:
    provider = OllamaProvider(OllamaConfig())
    if not await provider.healthy():
        pytest.skip("ollama not running")
    result = await provider.generate(
        "What is the capital of France? Respond as JSON.", Answer
    )
    assert "paris" in result.city.lower()
