import json

import httpx
import pytest
from pydantic import BaseModel

from lore.config import OllamaConfig
from lore.inference.ollama import OllamaProvider


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
