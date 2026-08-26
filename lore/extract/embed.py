"""Write the four artifacts a unit produces, identically in both tiers.

Takes primitives rather than a WorkUnit so the Kaggle kernel — which
imports this module from the package shipped inside the work-unit
dataset — never has to import lore.orchestrator.workunit too.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from lore.extract.runner import ExtractionResult


def write_unit_outputs(
    dest: Path,
    *,
    unit_id: str,
    chunk_ids: list[str],
    result: ExtractionResult,
    vectors: list[list[float]],
    model: str,
    embed_model: str,
    gpu_seconds: float,
    schema_version: str,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    columns: dict[str, list] = {
        key: []
        for key in (
            "chunk_id", "subject", "predicate", "object", "evidence",
            "confidence", "entity_names", "extractor_model", "run_id",
        )
    }
    for item in result.results:
        names = [entity.name for entity in item.extraction.entities]
        for triplet in item.extraction.triplets:
            columns["chunk_id"].append(item.chunk_id)
            columns["subject"].append(triplet.subject)
            columns["predicate"].append(triplet.predicate)
            columns["object"].append(triplet.object)
            columns["evidence"].append(triplet.evidence)
            columns["confidence"].append(triplet.confidence)
            columns["entity_names"].append(names)
            columns["extractor_model"].append(model)
            columns["run_id"].append(unit_id)
    pq.write_table(pa.table(columns), dest / "triplets.parquet")
    pq.write_table(
        pa.table({
            "chunk_id": chunk_ids,
            "vector": vectors,
            "embed_model": [embed_model] * len(chunk_ids),
        }),
        dest / "embeddings.parquet",
    )
    with (dest / "dead_letter.jsonl").open("w") as handle:
        for dead in result.dead_letters:
            handle.write(
                json.dumps({
                    "chunk_id": dead.chunk_id,
                    "raw_output": dead.raw_output,
                    "error": dead.error,
                })
                + "\n"
            )
    (dest / "manifest.json").write_text(
        json.dumps({
            "unit_id": unit_id,
            "chunks_in": len(chunk_ids),
            "chunks_ok": len(result.results),
            "chunks_dead": len(result.dead_letters),
            "gpu_seconds": gpu_seconds,
            "model": model,
            "embed_model": embed_model,
            "schema_version": schema_version,
        }, indent=2)
    )
