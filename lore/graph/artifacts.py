"""Read extraction artifacts — but only those a job row vouches for.

A failed unit legitimately leaves logs and partially pulled files behind,
so file presence is not evidence that a unit's work is complete. The
`jobs` table is the authority.

`validate_output` (lore/orchestrator/workunit.py) refuses to let a unit
reach `completed` unless every artifact file is already on disk. So a
`completed` job row whose directory or Parquet file has since gone
missing is never a normal, expected state — it means the extraction
directory was corrupted or partially deleted after the fact. Silently
dropping such a unit would let the graph loader write a fraction of a
book and report success, so we fail loudly instead.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow.parquet as pq

from lore.config import LoreConfig

JOB_KIND = "extract_unit"


class MissingArtifactError(RuntimeError):
    """A job row vouches for a unit whose artifact is gone from disk."""


def completed_unit_dirs(
    conn: sqlite3.Connection, config: LoreConfig, book_id: str
) -> list[Path]:
    rows = conn.execute(
        "SELECT detail FROM jobs WHERE kind = ? AND book_id = ? AND state = 'completed'"
        " ORDER BY detail",
        (JOB_KIND, book_id),
    ).fetchall()
    base = config.extractions_dir / book_id
    dirs: list[Path] = []
    for (unit_id,) in rows:
        if not unit_id:
            # `detail` holds the unit id and is minted by `pack_units`, so
            # a NULL one means the row was written by something else. Left
            # alone it reached `base / None` and surfaced as a bare
            # TypeError — a LORE crash rather than a diagnosis.
            raise MissingArtifactError(
                f"a completed {JOB_KIND!r} job for book {book_id} has no unit"
                " id in its `detail` column, so its artifacts cannot be"
                " located. The jobs table is corrupt for this book."
            )
        d = base / unit_id
        if not d.is_dir():
            raise MissingArtifactError(
                f"unit {unit_id!r} is marked completed but its directory is"
                f" missing: {d}. Re-run `lore extract {book_id} --resume`."
            )
        dirs.append(d)
    return dirs


def _read(dirs: list[Path], filename: str) -> list[dict]:
    rows: list[dict] = []
    for directory in dirs:
        path = directory / filename
        if not path.is_file():
            raise MissingArtifactError(
                f"unit {directory.name!r} is marked completed but its"
                f" {filename} is missing: {path}. Re-run"
                f" `lore extract {directory.parent.name} --resume`."
            )
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def read_triplets(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "triplets.parquet")


def read_embeddings(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "embeddings.parquet")
