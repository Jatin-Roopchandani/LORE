"""Local Ollama provider — structured output via /api/chat `format` schema."""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from lore.config import OllamaConfig
from lore.extract.schemas import MAX_OUTPUT_TOKENS
from lore.inference.base import ProviderOutputError

# A raw response within ~5% of this many characters is plausibly a
# truncated MAX_OUTPUT_TOKENS-token completion rather than a malformed
# one. This mirrors the ~3.1-3.4 chars/token ratio measured across the
# 26 dead letters that motivated raising the cap (6367-8329 characters
# for ~1800-2400 tokens): a cheap diagnostic hint, not a guarantee.
_CHARS_PER_TOKEN = 3.2
_TRUNCATION_HINT_CHARS = MAX_OUTPUT_TOKENS * _CHARS_PER_TOKEN


class OllamaProvider:
    def __init__(self, config: OllamaConfig, *, timeout: float = 120.0) -> None:
        self._model = config.model
        self._client = httpx.AsyncClient(base_url=config.host, timeout=timeout)

    async def generate(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        options: dict | None = None,
    ) -> BaseModel:
        # `options` is merged over the default, caller keys winning. A
        # decision that must be reproducible passes temperature 0 AND
        # seed 0: see lore/graph/adjudicate.py for why both are needed.
        response = await self._client.post(
            "/api/chat",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "format": schema.model_json_schema(),
                "stream": False,
                "options": {"num_predict": MAX_OUTPUT_TOKENS, **(options or {})},
            },
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        try:
            return schema.model_validate_json(content)
        except Exception as exc:
            message = str(exc)
            if abs(len(content) - _TRUNCATION_HINT_CHARS) <= 0.05 * _TRUNCATION_HINT_CHARS:
                message = (
                    f"{message} (raw output is {len(content)} chars, near the "
                    f"~{_TRUNCATION_HINT_CHARS:.0f}-char span for a "
                    f"MAX_OUTPUT_TOKENS={MAX_OUTPUT_TOKENS} completion - this "
                    "looks like truncation, not malformed JSON)"
                )
            raise ProviderOutputError(message, raw_output=content) from exc

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        response = await self._client.post(
            "/api/embed", json={"model": model, "input": texts}
        )
        response.raise_for_status()
        return response.json()["embeddings"]

    async def healthy(self) -> bool:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
