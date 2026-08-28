"""Read extraction artifacts — but only those a job row vouches for.

A failed unit legitimately leaves logs and partially pulled files behind,
so file presence is not evidence that a unit's work is complete. The
`jobs` table is the authority.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow.parquet as pq

from lore.config import LoreConfig

JOB_KIND = "extract_unit"


def completed_unit_dirs(
    conn: sqlite3.Connection, config: LoreConfig, book_id: str
) -> list[Path]:
    rows = conn.execute(
        "SELECT detail FROM jobs WHERE kind = ? AND book_id = ? AND state = 'completed'"
        " ORDER BY detail",
        (JOB_KIND, book_id),
    ).fetchall()
    base = config.extractions_dir / book_id
    return [base / row[0] for row in rows if (base / row[0]).is_dir()]


def _read(dirs: list[Path], filename: str) -> list[dict]:
    rows: list[dict] = []
    for directory in dirs:
        path = directory / filename
        if path.is_file():
            rows.extend(pq.read_table(path).to_pylist())
    return rows


def read_triplets(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "triplets.parquet")


def read_embeddings(dirs: list[Path]) -> list[dict]:
    return _read(dirs, "embeddings.parquet")
