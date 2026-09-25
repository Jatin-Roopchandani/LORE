"""Work units run from this machine rather than on Kaggle.

Two tiers share one write-and-validate step:

  * `run_unit_local`: fully local on Ollama, zero egress.
  * `run_unit_gemini`: generation on Gemini, one chunk per request, with
    embeddings still local on Ollama (nomic-embed-text with
    DOCUMENT_PREFIX, the same embedder the query side uses).

Both write the kernel-shaped artifacts and check them with the same
`validate_output` the Kaggle path uses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lore.config import LoreConfig
from lore.extract.embed import DOCUMENT_PREFIX, write_unit_outputs
from lore.extract.runner import (
    ChunkInput,
    ExtractionHalted,
    ExtractionResult,
    extract_chunks,
)
from lore.extract.schemas import SCHEMA_VERSION
from lore.inference.gemini import (
    GeminiClient,
    GeminiError,
    GeminiOutputError,
    default_key_file,
    resolve_api_key,
)
from lore.inference.ollama import OllamaProvider
from lore.orchestrator.gemini_budget import recorder_for
from lore.orchestrator.workunit import OutputSummary, WorkUnit, validate_output

GEMINI_EXHAUSTED_HINT = (
    "Nothing was dead-lettered and this unit is marked failed; run"
    " `lore extract <book_id> --resume` (or `lore process <book_id> --resume`)"
    " when a daily budget resets."
)


def _finish_unit(
    config: LoreConfig,
    unit: WorkUnit,
    dest: Path,
    *,
    result: ExtractionResult,
    vectors: list[list[float]],
    model: str,
    started: float,
    started_at: str,
) -> OutputSummary:
    write_unit_outputs(
        dest,
        unit_id=unit.unit_id,
        book_id=unit.book_id,
        chunk_ids=[chunk.id for chunk in unit.chunks],
        result=result,
        vectors=vectors,
        model=model,
        embed_model=config.extraction.embed_model_ollama,
        gpu_seconds=time.monotonic() - started,
        schema_version=SCHEMA_VERSION,
        started_at=started_at,
    )
    return validate_output(unit, dest)


async def run_unit_local(
    config: LoreConfig, unit: WorkUnit, dest: Path, provider=None
) -> OutputSummary:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    owned = provider is None
    active = provider if provider is not None else OllamaProvider(config.ollama)
    try:
        inputs = [ChunkInput(chunk.id, chunk.text) for chunk in unit.chunks]
        result = await extract_chunks(inputs, active)
        vectors = await active.embed(
            [DOCUMENT_PREFIX + chunk.text for chunk in unit.chunks],
            model=config.extraction.embed_model_ollama,
        )
    finally:
        if owned:
            await active.aclose()
    return _finish_unit(
        config, unit, dest, result=result, vectors=vectors,
        model=config.ollama.model, started=started, started_at=started_at,
    )


class GeminiExtractionProvider:
    """`extract_chunks`' provider seam over `GeminiClient.generate_json`.

    Its job is to sort Gemini's failures into the two kinds the runner
    treats differently:

      * GeminiOutputError (a model answered with empty or schema-invalid
        output) is about this chunk, so it is re-raised as-is: the runner
        retries it, then dead-letters the chunk, exactly as for Ollama.
      * Every other GeminiError means no model can serve ANY chunk right
        now: every model refused (429/503/404), the key was rejected
        (401/403), the API was unreachable, or an unexpected HTTP error. It
        becomes ExtractionHalted, which the runner never retries and never
        dead-letters. The unit fails and `--resume` runs it again.

    `answered_by` collects the model of every response, so the manifest
    names the model(s) that actually wrote the unit.
    """

    def __init__(self, client: GeminiClient) -> None:
        self._client = client
        self.answered_by: set[str] = set()

    async def generate(self, prompt: str, schema):
        try:
            extraction = await self._client.generate_json(
                prompt, schema, exhausted_hint=GEMINI_EXHAUSTED_HINT
            )
        except GeminiOutputError:
            if self._client.last_model:
                self.answered_by.add(self._client.last_model)
            raise
        except GeminiError as exc:
            raise ExtractionHalted(f"Gemini cannot extract right now: {exc}") from exc
        if self._client.last_model:
            self.answered_by.add(self._client.last_model)
        return extraction

    def model_label(self) -> str:
        """One model id, or a sorted comma-joined list if the cascade fell
        through. A string, not a JSON list, because `manifest.model` and the
        triplets' `extractor_model` column are both strings."""
        return ",".join(sorted(self.answered_by)) or self._client.models[0]


async def run_unit_gemini(
    config: LoreConfig,
    unit: WorkUnit,
    dest: Path,
    *,
    client: GeminiClient,
    embedder,
) -> OutputSummary:
    """One unit: embed locally, then one Gemini request per chunk, in order.

    Embedding runs FIRST. The output is the same either way, but a stopped
    Ollama then fails the unit before any of the day's Gemini requests are
    spent on chunks whose unit could not be completed.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        vectors = await embedder.embed(
            [DOCUMENT_PREFIX + chunk.text for chunk in unit.chunks],
            model=config.extraction.embed_model_ollama,
        )
    except Exception as exc:
        raise ExtractionHalted(
            f"the local embedder ({config.extraction.embed_model_ollama} on"
            f" {config.ollama.host}) failed, so no Gemini request was spent on"
            f" this unit: {exc}"
        ) from exc
    generator = GeminiExtractionProvider(client)
    result = await extract_chunks(
        [ChunkInput(chunk.id, chunk.text) for chunk in unit.chunks], generator
    )
    return _finish_unit(
        config, unit, dest, result=result, vectors=vectors,
        model=generator.model_label(), started=started, started_at=started_at,
    )


@dataclass
class GeminiTier:
    """The clients one `run_extraction` call shares across all its units.

    One GeminiClient per run, so per-model RPM throttling holds across unit
    boundaries. Its httpx pool is bound to the event loop that first used
    it, so the caller must run every unit and `aclose` on one loop.
    """

    config: LoreConfig
    client: GeminiClient
    embedder: object
    _closed: bool = field(default=False, repr=False)

    async def run(self, unit: WorkUnit, dest: Path) -> OutputSummary:
        return await run_unit_gemini(
            self.config, unit, dest, client=self.client, embedder=self.embedder
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.client.aclose()
        finally:
            await self.embedder.aclose()


def missing_gemini_key_message(settings: list[str], alternative: str) -> str:
    """The refusal when a step is on Gemini and no key exists, in `ask`'s style."""
    return (
        f"{' and '.join(settings)} but no API key: set GEMINI_API_KEY (or"
        " GOOGLE_API_KEY), or put one line `GEMINI_API_KEY=...` in"
        f" ~/.config/lore/gemini.env with mode 600. Or {alternative}."
    )


def make_gemini_tier(config: LoreConfig, api_key: str | None = None) -> GeminiTier:
    """Build the run's tier. Raises ValueError, opening nothing, without a key."""
    key = api_key or resolve_api_key(key_file=default_key_file())
    if not key:
        raise ValueError(missing_gemini_key_message(
            ['extraction_provider = "gemini"'],
            'set extraction_provider = "kaggle" or "ollama"',
        ))
    return GeminiTier(
        config=config,
        client=GeminiClient(key, models=config.extraction.gemini_models,
                            recorder=recorder_for(config.sqlite_path)),
        embedder=OllamaProvider(config.ollama),
    )
