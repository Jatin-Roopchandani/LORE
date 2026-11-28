"""Local Ollama provider — structured output via /api/chat `format` schema."""
from __future__ import annotations

import os

import httpx
from pydantic import BaseModel

from lore.config import OllamaConfig
from lore.db.lance_store import EMBED_DIM
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
    _embed_client: httpx.AsyncClient | None = None

    def __init__(self, config: OllamaConfig, *, timeout: float = 120.0) -> None:
        self._model = config.model
        self._client = httpx.AsyncClient(base_url=config.host, timeout=timeout)
        if config.embed_base_url:
            key = os.environ.get("LORE_EMBED_API_KEY")
            self._embed_client = httpx.AsyncClient(
                base_url=config.embed_base_url, timeout=timeout,
                headers={"Authorization": f"Bearer {key}"} if key else None)

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
        if self._embed_client is not None:
            # The stores' vectors are EMBED_DIM wide; a Matryoshka model
            # truncates to it on the server.
            response = await self._embed_client.post(
                "/embeddings",
                json={"model": model, "input": texts, "dimensions": EMBED_DIM})
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            return [row["embedding"] for row in rows]
        response = await self._client.post(
            "/api/embed", json={"model": model, "input": texts}
        )
        response.raise_for_status()
        return response.json()["embeddings"]

    async def healthy(self) -> bool:
        try:
            if self._embed_client is not None:
                response = await self._embed_client.get("/models", timeout=5.0)
            else:
                response = await self._client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._embed_client is not None:
            await self._embed_client.aclose()


class ModelPullError(RuntimeError):
    """The embedding model could not be made available on that Ollama."""


def _has_model(tags: dict, name: str) -> bool:
    pulled = {m.get("name", "") for m in tags.get("models", [])}
    return name in pulled or f"{name}:latest" in pulled


def ensure_model(host: str, name: str, *, wait_s: float = 300.0, client: httpx.Client | None = None,
                 sleep=None, now=None) -> bool:
    """Make sure the Ollama at `host` has model `name`, pulling it through
    Ollama's own HTTP API when it is missing. Waits up to `wait_s` for the
    server to answer (it may still be starting). Returns True when it pulled,
    False when the model was already there; raises ModelPullError otherwise.

    The self-hosted compose file runs this (`lore pull-models`) on LORE's own
    image, so an Ollama you already run is enough: no Ollama image is needed
    just to fetch the model."""
    import time
    sleep = sleep or time.sleep
    now = now or time.monotonic
    own = client is None
    client = client or httpx.Client(base_url=host.rstrip("/"), timeout=30.0)
    try:
        deadline = now() + wait_s
        while True:
            try:
                tags = client.get("/api/tags").json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                if now() >= deadline:
                    raise ModelPullError(
                        f"no Ollama answered at {host} within {int(wait_s)}s ({exc}). "
                        "Is it running, and is LORE_OLLAMA_HOST right?") from exc
                sleep(3.0)
        if _has_model(tags, name):
            return False
        try:
            r = client.post("/api/pull", json={"model": name, "stream": False},
                            timeout=httpx.Timeout(30.0, read=1800.0))
        except httpx.HTTPError as exc:
            raise ModelPullError(f"pulling {name} on {host} failed: {exc}") from exc
        if r.status_code >= 400:
            raise ModelPullError(f"pulling {name} on {host} failed: HTTP {r.status_code} "
                                 f"{r.text[:200]}")
        if not _has_model(client.get("/api/tags").json(), name):
            raise ModelPullError(f"{host} reported success but does not list {name}")
        return True
    finally:
        if own:
            client.close()
