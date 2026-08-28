"""Per-chunk extraction loop, independent of which backend runs the model.

This is the code the Kaggle kernel imports and the local Ollama tier
calls, so both tiers share prompt, validation, retry and dead-letter
behaviour exactly (SPEC.md section 3).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from lore.extract.prompts import build_prompt
from lore.extract.schemas import ChunkExtraction


@dataclass(frozen=True)
class ChunkInput:
    chunk_id: str
    text: str


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    extraction: ChunkExtraction


@dataclass(frozen=True)
class DeadLetter:
    chunk_id: str
    raw_output: str
    error: str


@dataclass
class ExtractionResult:
    results: list[ChunkResult] = field(default_factory=list)
    dead_letters: list[DeadLetter] = field(default_factory=list)


def _raw_output_of(exc: Exception) -> str:
    """Providers may attach the model's raw text; otherwise we log nothing."""
    return str(getattr(exc, "raw_output", "") or "")


async def extract_chunks(
    chunks: list[ChunkInput],
    provider,
    *,
    max_attempts: int = 2,
    on_progress: Callable[[int, int], None] | None = None,
) -> ExtractionResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    result = ExtractionResult()
    total = len(chunks)
    for done, chunk in enumerate(chunks, start=1):
        prompt = build_prompt(chunk.text)
        last_error: Exception | None = None
        for _ in range(max_attempts):
            try:
                extraction = await provider.generate(prompt, ChunkExtraction)
            except Exception as exc:  # provider or validation failure
                last_error = exc
                continue
            result.results.append(ChunkResult(chunk.chunk_id, extraction))
            last_error = None
            break
        if last_error is not None:
            result.dead_letters.append(
                DeadLetter(
                    chunk_id=chunk.chunk_id,
                    raw_output=_raw_output_of(last_error),
                    error=str(last_error),
                )
            )
        if on_progress is not None:
            on_progress(done, total)
    return result
