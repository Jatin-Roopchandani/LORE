"""Tier 2: run a work unit entirely on this machine, zero egress."""
from __future__ import annotations

import time
from pathlib import Path

from lore.config import LoreConfig
from lore.extract.embed import write_unit_outputs
from lore.extract.runner import ChunkInput, extract_chunks
from lore.inference.ollama import OllamaProvider
from lore.orchestrator.workunit import OutputSummary, WorkUnit, validate_output


async def run_unit_local(
    config: LoreConfig, unit: WorkUnit, dest: Path, provider=None
) -> OutputSummary:
    started = time.monotonic()
    owned = provider is None
    active = provider if provider is not None else OllamaProvider(config.ollama)
    try:
        inputs = [ChunkInput(chunk.id, chunk.text) for chunk in unit.chunks]
        result = await extract_chunks(inputs, active)
        vectors = await active.embed(
            [chunk.text for chunk in unit.chunks],
            model=config.extraction.embed_model_ollama,
        )
    finally:
        if owned:
            await active.aclose()
    write_unit_outputs(
        unit, dest, result, vectors,
        model=config.ollama.model,
        embed_model=config.extraction.embed_model_ollama,
        gpu_seconds=time.monotonic() - started,
    )
    return validate_output(unit, dest)
