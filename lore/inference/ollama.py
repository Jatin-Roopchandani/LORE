"""Local Ollama provider — structured output via /api/chat `format` schema."""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from lore.config import OllamaConfig


class OllamaProvider:
    def __init__(self, config: OllamaConfig) -> None:
        self._model = config.model
        self._client = httpx.AsyncClient(base_url=config.host, timeout=120.0)

    async def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        response = await self._client.post(
            "/api/chat",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema.model_json_schema(),
                "stream": False,
            },
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        return schema.model_validate_json(content)

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
