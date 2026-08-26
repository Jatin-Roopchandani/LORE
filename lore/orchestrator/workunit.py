"""Work units: the transport format between the local pipeline and Kaggle."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from lore.extract.schemas import SCHEMA_VERSION
from lore.ingest.models import Chunk

_ARTIFACTS = ("triplets.parquet", "embeddings.parquet", "manifest.json")


class OutputError(RuntimeError):
    """A returned unit failed validation and must be re-queued."""


@dataclass(frozen=True)
class WorkUnit:
    unit_id: str
    book_id: str
    chunks: list[Chunk]


@dataclass(frozen=True)
class OutputSummary:
    chunks_in: int
    chunks_ok: int
    chunks_dead: int
    gpu_seconds: float
    model: str


def pack_units(book_id: str, chunks: list[Chunk], size: int) -> list[WorkUnit]:
    if size < 1:
        raise ValueError("work unit size must be >= 1")
    units: list[WorkUnit] = []
    for index, start in enumerate(range(0, len(chunks), size)):
        units.append(
            WorkUnit(
                unit_id=f"{book_id}-u{index:04d}",
                book_id=book_id,
                chunks=chunks[start : start + size],
            )
        )
    return units


def write_unit(
    unit: WorkUnit, dest: Path, package_root: Path | None = None
) -> Path:
    """Materialize a unit's upload directory: chunks, manifest, and the
    `lore` package the kernel imports so both tiers share one runner."""
    unit_dir = dest / unit.unit_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    with (unit_dir / "unit.jsonl").open("w") as handle:
        for chunk in unit.chunks:
            handle.write(
                json.dumps(
                    {
                        "chunk_id": chunk.id,
                        "text": chunk.text,
                        "chapter_seq": chunk.chapter_seq,
                    }
                )
                + "\n"
            )
    (unit_dir / "unit-manifest.json").write_text(
        json.dumps(
            {
                "unit_id": unit.unit_id,
                "book_id": unit.book_id,
                "chunk_count": len(unit.chunks),
                "schema_version": SCHEMA_VERSION,
            },
            indent=2,
        )
    )
    root = package_root if package_root is not None else Path(__file__).resolve().parents[1]
    shutil.copytree(
        root,
        unit_dir / "lore",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return unit_dir


def validate_output(unit: WorkUnit, out_dir: Path) -> OutputSummary:
    missing = [name for name in _ARTIFACTS if not (out_dir / name).is_file()]
    if missing:
        raise OutputError(f"unit {unit.unit_id}: missing artifacts {missing}")

    manifest = json.loads((out_dir / "manifest.json").read_text())
    expected = {chunk.id for chunk in unit.chunks}
    chunks_in = int(manifest.get("chunks_in", -1))
    chunks_ok = int(manifest.get("chunks_ok", -1))
    chunks_dead = int(manifest.get("chunks_dead", -1))
    if chunks_in != len(expected):
        raise OutputError(
            f"unit {unit.unit_id}: manifest says {chunks_in} chunks in, "
            f"expected {len(expected)}"
        )
    if chunks_ok + chunks_dead != chunks_in:
        raise OutputError(
            f"unit {unit.unit_id}: {chunks_ok} ok + {chunks_dead} dead are not "
            f"accounted for out of {chunks_in} chunks"
        )
    embeddings = pq.read_table(out_dir / "embeddings.parquet", columns=["chunk_id"])
    embedding_ids = embeddings.column("chunk_id").to_pylist()
    embedding_id_set = set(embedding_ids)
    missing_embeddings = expected - embedding_id_set
    if missing_embeddings:
        raise OutputError(
            f"unit {unit.unit_id}: embeddings.parquet is missing chunk ids "
            f"{sorted(missing_embeddings)[:3]}"
        )
    foreign_embeddings = embedding_id_set - expected
    if foreign_embeddings:
        raise OutputError(
            f"unit {unit.unit_id}: embeddings.parquet contains foreign chunk ids "
            f"{sorted(foreign_embeddings)[:3]}"
        )
    if len(embedding_ids) != chunks_in:
        raise OutputError(
            f"unit {unit.unit_id}: embeddings.parquet has {len(embedding_ids)} rows, "
            f"expected {chunks_in}"
        )

    triplets = pq.read_table(out_dir / "triplets.parquet", columns=["chunk_id"])
    foreign_triplets = set(triplets.column("chunk_id").to_pylist()) - expected
    if foreign_triplets:
        raise OutputError(
            f"unit {unit.unit_id}: triplets.parquet contains foreign chunk ids "
            f"{sorted(foreign_triplets)[:3]}"
        )
    return OutputSummary(
        chunks_in=chunks_in,
        chunks_ok=chunks_ok,
        chunks_dead=chunks_dead,
        gpu_seconds=float(manifest.get("gpu_seconds", 0.0)),
        model=str(manifest.get("model", "")),
    )
