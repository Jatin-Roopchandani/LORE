"""Work units, run from this machine.

Two tiers share one write-and-validate step:

  * `run_unit_local`: fully local on Ollama, zero egress.
  * `run_unit_gemini`: generation on Gemini, one chunk per request, with
    embeddings still local on Ollama (nomic-embed-text with
    DOCUMENT_PREFIX, the same embedder the query side uses).

Both write the same four artifacts and check them with `validate_output`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from lore import fs
from lore.config import LoreConfig
from lore.extract.embed import DOCUMENT_PREFIX, write_unit_outputs
from lore.extract.runner import (
    ChunkInput,
    ChunkResult,
    DeadLetter,
    ExtractionHalted,
    ExtractionResult,
    extract_chunks,
)
from lore.extract.schemas import SCHEMA_VERSION, ChunkExtraction
from lore.inference.gemini import (
    GeminiClient,
    GeminiError,
    GeminiOutputError,
    GeminiQuotaError,
    default_key_file,
    resolve_api_key,
)
from lore.inference.key_scope import gemini_api_key  # noqa: F401 - a patch point
from lore.inference.providers import factory as providers
from lore.inference.ollama import OllamaProvider
from lore.orchestrator.concurrency import bounded_fetch, llm_concurrency
from lore.orchestrator.gemini_budget import recorder_for
from lore.orchestrator.pipeline import QuotaStop
from lore.orchestrator.workunit import OutputSummary, WorkUnit, validate_output

GEMINI_EXHAUSTED_HINT = (
    "Nothing was dead-lettered and this unit is marked failed; run"
    " `lore extract <book_id> --resume` (or `lore process <book_id> --resume`)"
    " when a daily budget resets."
)


class QuotaHalted(ExtractionHalted):
    """ExtractionHalted because the day's Gemini allowance is spent (every
    model refused, or the processing reserve was reached) rather than
    because of a rejected key or an unreachable server. Resumable processing
    turns it into a QuotaStop; plain `lore extract` treats it as any halt."""


PARTS_DIR = "parts"

log = logging.getLogger(__name__)


def parts_dir(dest: Path) -> Path:
    return dest / PARTS_DIR


def parts_done(dest: Path, unit: WorkUnit) -> int:
    """How many of the unit's chunks already have a saved, readable part."""
    folder = parts_dir(dest)
    return sum(_load_part(folder, chunk.id) is not None for chunk in unit.chunks)


def _write_part(folder: Path, chunk_id: str, result: ExtractionResult,
                models: set[str]) -> None:
    """One chunk's outcome (extraction or dead letter), saved atomically and
    durably (lore.fs), so neither a stop nor a machine kill leaves a half part."""
    if result.results:
        body = {"extraction": result.results[0].extraction.model_dump(mode="json"),
                "dead": None}
    else:
        dead = result.dead_letters[0]
        body = {"extraction": None,
                "dead": {"raw_output": dead.raw_output, "error": dead.error}}
    body["models"] = sorted(models)
    fs.atomic_write_json(folder / f"{chunk_id}.json", body)


def _load_part(folder: Path, chunk_id: str) -> tuple[ExtractionResult, set[str]] | None:
    """A saved part, or None when there is none to use.

    A part that is empty, truncated or does not validate (what a hard kill
    could leave before writes were fsynced) is treated as missing: logged
    once, removed, and its chunk requested again. Never a crash.
    """
    path = folder / f"{chunk_id}.json"
    if not path.is_file():
        return None
    try:
        return _read_part(folder, chunk_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("part %s is damaged (%s); its chunk will be extracted again",
                    path, exc)
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _read_part(folder: Path, chunk_id: str) -> tuple[ExtractionResult, set[str]]:
    body = json.loads((folder / f"{chunk_id}.json").read_text())
    if not isinstance(body, dict):
        raise ValueError("a part is a JSON object")
    result = ExtractionResult()
    if body["extraction"] is not None:
        result.results.append(ChunkResult(
            chunk_id, ChunkExtraction.model_validate(body["extraction"])))
    else:
        result.dead_letters.append(DeadLetter(
            chunk_id, body["dead"]["raw_output"], body["dead"]["error"]))
    return result, set(body["models"])


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

    def __init__(self, client: GeminiClient,
                 before_request: Callable[[], None] | None = None) -> None:
        self._client = client
        self._before_request = before_request
        self.answered_by: set[str] = set()

    async def generate(self, prompt: str, schema):
        if self._before_request is not None:
            try:
                self._before_request()
            except QuotaStop as stop:
                raise QuotaHalted(str(stop)) from stop
        try:
            extraction = await self._client.generate_json(
                prompt, schema, exhausted_hint=GEMINI_EXHAUSTED_HINT
            )
        except GeminiOutputError as exc:
            # The model the error names: `last_model` is shared by every
            # request in flight on this client, and an unusable reply raised
            # before the cascade finished never set it for this one.
            model = getattr(exc, "model", "") or self._client.last_model
            if model:
                self.answered_by.add(model)
            raise
        except GeminiQuotaError as exc:
            raise QuotaHalted(f"Gemini cannot extract right now: {exc}") from exc
        except GeminiError as exc:
            raise ExtractionHalted(f"Gemini cannot extract right now: {exc}") from exc
        # Read with no await since the call returned, so it is this
        # request's model even with others in flight on the same client.
        if self._client.last_model:
            self.answered_by.add(self._client.last_model)
        return extraction

    def model_label(self) -> str:
        """One model id, or a sorted comma-joined list if the cascade fell
        through. A string, not a JSON list, because `manifest.model` and the
        triplets' `extractor_model` column are both strings."""
        return ",".join(sorted(self.answered_by)) or self._client.models[0]


async def embed_unit(config: LoreConfig, unit: WorkUnit, embedder) -> list[list[float]]:
    """The unit's chunk vectors in ONE local embed request, or ExtractionHalted."""
    try:
        return await embedder.embed(
            [DOCUMENT_PREFIX + chunk.text for chunk in unit.chunks],
            model=config.extraction.embed_model_ollama,
        )
    except Exception as exc:
        raise ExtractionHalted(
            f"the local embedder ({config.extraction.embed_model_ollama} on"
            f" {config.ollama.host}) failed, so no Gemini request was spent on"
            f" this unit: {exc}"
        ) from exc


async def run_unit_gemini(
    config: LoreConfig,
    unit: WorkUnit,
    dest: Path,
    *,
    client: GeminiClient,
    embedder=None,
    before_request: Callable[[], None] | None = None,
    on_chunk: Callable[[int], None] | None = None,
    concurrency: int | None = None,
    vectors: list[list[float]] | None = None,
) -> OutputSummary:
    """One unit: embed locally, then one model request per chunk.

    Embedding runs FIRST (or was done already: `vectors`, see GeminiTier).
    The output is the same either way, but a stopped Ollama then fails the
    unit before any of the day's requests are spent on chunks whose unit
    could not be completed.

    Up to `concurrency` chunk requests are in flight at once (default:
    `llm_concurrency(config)`, 1 on free-tier Gemini). Each chunk's outcome
    is saved to `<dest>/parts/<chunk_id>.json` as soon as it returns; a
    chunk that has a part is not requested again, so a unit stopped midway
    resumes with only its missing chunks. Once every chunk has a part the
    usual artifacts are assembled from them in chunk order, so they do not
    depend on the order the answers arrived in.

    `before_request` runs before each request (it may raise QuotaStop). A
    stop, or a provider that cannot serve (ExtractionHalted), lets no
    further request start; the ones in flight finish and are saved, then
    the first failure in chunk order is raised. `on_chunk(n)` reports n
    chunks of this unit done (absolute, rising by one per saved part).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    if vectors is None:
        vectors = await embed_unit(config, unit, embedder)
    limit = concurrency if concurrency is not None else llm_concurrency(config)
    folder = parts_dir(dest)
    done = parts_done(dest, unit)
    if on_chunk is not None:
        on_chunk(done)
    parts: dict[str, tuple[ExtractionResult, set[str]]] = {}
    missing = []
    for chunk in unit.chunks:
        saved = _load_part(folder, chunk.id)
        if saved is not None:
            parts[chunk.id] = saved
        else:
            missing.append(chunk)

    async def extract_one(chunk) -> None:
        nonlocal done
        generator = GeminiExtractionProvider(client, before_request)
        one = await extract_chunks([ChunkInput(chunk.id, chunk.text)], generator)
        # No await from here on: a cancellation can never leave half a part.
        _write_part(folder, chunk.id, one, generator.answered_by)
        # Read back, so a fresh chunk and a resumed one assemble identically.
        parts[chunk.id] = _read_part(folder, chunk.id)
        done += 1
        if on_chunk is not None:
            on_chunk(done)

    async with bounded_fetch(missing, extract_one, limit) as pending:
        for chunk in missing:
            await pending[chunk]
    result = ExtractionResult()
    models: set[str] = set()
    for chunk in unit.chunks:
        part, part_models = parts[chunk.id]
        result.results.extend(part.results)
        result.dead_letters.extend(part.dead_letters)
        models |= part_models
    model = ",".join(sorted(models)) or client.models[0]
    return _finish_unit(
        config, unit, dest, result=result, vectors=vectors,
        model=model, started=started, started_at=started_at,
    )


@dataclass
class GeminiTier:
    """The clients one `run_extraction` call shares across all its units.

    One model client per run, so per-model RPM throttling holds across unit
    boundaries. Its httpx pool is bound to the event loop that first used
    it, so the caller must run every unit and `aclose` on one loop.

    `run(unit, ..., next_unit=...)` overlaps the CPU and the model: once
    this unit's own embedding has succeeded, the next unit's embedding
    starts in the background while this unit's chunks are extracted. One
    embed at a time (a CPU Ollama serializes them anyway), and a unit's
    requests start only after its own embedding succeeded. `concurrency`
    defaults to `llm_concurrency(config)`.
    """

    config: LoreConfig
    client: GeminiClient
    embedder: object
    concurrency: int | None = None
    _closed: bool = field(default=False, repr=False)
    _prefetch: tuple[str, asyncio.Future] | None = field(default=None, repr=False)

    async def _vectors(self, unit: WorkUnit) -> list[list[float]]:
        ahead, self._prefetch = self._prefetch, None
        if ahead is not None:
            unit_id, task = ahead
            if unit_id == unit.unit_id:
                return await task
            await _cancel(task)
        return await embed_unit(self.config, unit, self.embedder)

    async def run(self, unit: WorkUnit, dest: Path, *,
                  before_request: Callable[[], None] | None = None,
                  on_chunk: Callable[[int], None] | None = None,
                  next_unit: WorkUnit | None = None) -> OutputSummary:
        vectors = await self._vectors(unit)
        if next_unit is not None:
            self._prefetch = (next_unit.unit_id, asyncio.ensure_future(
                embed_unit(self.config, next_unit, self.embedder)))
        concurrency = (self.concurrency if self.concurrency is not None
                       else llm_concurrency(self.config))
        return await run_unit_gemini(
            self.config, unit, dest, client=self.client, embedder=self.embedder,
            before_request=before_request, on_chunk=on_chunk,
            concurrency=concurrency, vectors=vectors,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        ahead, self._prefetch = self._prefetch, None
        try:
            if ahead is not None:
                await _cancel(ahead[1])
            await self.client.aclose()
        finally:
            await self.embedder.aclose()


async def _cancel(task: asyncio.Future) -> None:
    """Cancel a background embed and wait for it; its outcome is unwanted."""
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 - cancelled, or failed: either way unused
        pass


def missing_gemini_key_message(settings: list[str], alternative: str) -> str:
    """The refusal when a step is on Gemini and no key exists, in `ask`'s style."""
    return (
        f"{' and '.join(settings)} but no API key: set GEMINI_API_KEY (or"
        " GOOGLE_API_KEY), or put one line `GEMINI_API_KEY=...` in"
        f" ~/.config/lore/gemini.env with mode 600. Or {alternative}."
    )


def make_gemini_tier(config: LoreConfig, api_key: str | None = None) -> GeminiTier:
    """Build the run's tier. Raises ValueError, opening nothing, without a key."""
    # A bound user's provider and key in multi mode, never the file
    # (key_scope); [lore.llm]'s provider in local mode (providers.factory).
    creds = providers.credentials(
        config, api_key, local=lambda: resolve_api_key(key_file=default_key_file()))
    if not creds.key:
        raise ValueError(missing_gemini_key_message(
            ['extraction_provider = "gemini"'],
            'set extraction_provider = "ollama"',
        ))
    gemini = creds.provider == providers.GEMINI
    return GeminiTier(
        config=config,
        client=providers.build(creds, config, "extract",
                               gemini_models=config.extraction.gemini_models,
                               recorder=recorder_for(config.sqlite_path) if gemini else None),
        embedder=OllamaProvider(config.ollama),
    )
