"""Read extraction artifacts — but only those a job row vouches for.

A failed unit legitimately leaves logs and partially pulled files behind,
so file presence is not evidence that a unit's work is complete. The
`jobs` table is the authority for what is done.

The files are the authority for what is still THERE. A unit reaches
`completed` only after its artifacts are written durably (lore.fs: temp,
fsync, rename) and validated. But a hard kill of the machine before that
fix, or a directory moved or deleted by hand, can still leave a
`completed` row whose directory is gone or whose files are empty or
unreadable. Such a unit is never loaded as a fraction of a book, and never
a dead end either: its row is demoted to `failed` (the reason kept in
`jobs.error`), and `DamagedUnitsError` is raised so the caller can send the
book back through extraction, which redoes exactly those units. The
resumable runner (`run_book`) does that by itself.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pyarrow.parquet as pq

from lore.config import LoreConfig

JOB_KIND = "extract_unit"

log = logging.getLogger(__name__)

# What the loader reads from a unit, and what extraction writes (manifest last).
LOADED = ("triplets.parquet", "embeddings.parquet")
WRITTEN = ("triplets.parquet", "embeddings.parquet", "dead_letter.jsonl", "manifest.json")


class MissingArtifactError(RuntimeError):
    """A job row vouches for a unit whose artifact is gone from disk."""


class DamagedUnitsError(MissingArtifactError):
    """Completed units whose files are missing, empty or unreadable.

    Their job rows have already been demoted to `failed`, so the next
    extraction run (or resume) re-extracts them, and only them.
    """

    def __init__(self, book_id: str, problems: dict[str, str]) -> None:
        self.book_id = book_id
        self.unit_ids = sorted(problems)
        first = self.unit_ids[0]
        super().__init__(
            f"{len(problems)} extraction unit(s) of book {book_id} had missing or"
            f" damaged files (e.g. {first}: {problems[first]}); they were marked"
            f" for re-extract. Continue the book (or `lore extract {book_id}"
            " --resume`) to redo them."
        )


def unit_problem(directory: Path, *, strict: bool = True) -> str | None:
    """Why a unit's files cannot be trusted, or None when they look whole.

    Cheap: parquet footers and the small manifest, never the whole table.
    `strict` (extraction's check) wants every file a unit writes, with the
    manifest's counts matching the files; the loader passes False and needs
    only the two parquet files it reads. A 0-byte file is damaged except
    `dead_letter.jsonl`, which is legitimately empty when nothing died.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return "its directory is missing"
    for name in (WRITTEN if strict else LOADED):
        path = directory / name
        if not path.is_file():
            return f"{name} is missing"
        if name != "dead_letter.jsonl" and path.stat().st_size == 0:
            return f"{name} is empty (0 bytes)"
    try:
        metadata = {name: pq.read_metadata(directory / name) for name in LOADED}
    except Exception as exc:  # noqa: BLE001 - any unreadable parquet is damage
        return f"a parquet file is unreadable: {exc}"
    if not strict:
        return None
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        chunks_in = int(manifest["chunks_in"])
        chunks_dead = int(manifest["chunks_dead"])
        dead_lines = [line for line in
                      (directory / "dead_letter.jsonl").read_text().splitlines()
                      if line.strip()]
        for line in dead_lines:
            json.loads(line)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"manifest.json or dead_letter.jsonl is unreadable: {exc}"
    if metadata["embeddings.parquet"].num_rows != chunks_in:
        return (f"embeddings.parquet has {metadata['embeddings.parquet'].num_rows}"
                f" rows, the manifest says {chunks_in} chunks")
    if len(dead_lines) != chunks_dead:
        return (f"dead_letter.jsonl has {len(dead_lines)} entries, the manifest"
                f" says {chunks_dead}")
    return None


def demote_units(conn: sqlite3.Connection, book_id: str,
                 problems: dict[str, str]) -> None:
    """Completed rows whose files disagree become `failed`: not done, so the
    next extraction (resume included) redoes them. Logged once, here."""
    with conn:
        for unit_id, why in problems.items():
            log.warning("extraction unit %s of book %s: %s; it will be re-extracted",
                        unit_id, book_id, why)
            conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?,"
                " updated_at = datetime('now')"
                " WHERE kind = ? AND detail = ? AND state = 'completed'",
                (f"files missing or damaged, re-extract: {why}"[:4000],
                 JOB_KIND, unit_id),
            )


def completed_units_with_problems(
    conn: sqlite3.Connection, config: LoreConfig, book_id: str,
    unit_ids: Iterable[str], *, strict: bool = True,
) -> dict[str, str]:
    """{unit_id: problem} for those of `unit_ids` whose files are not whole."""
    base = config.extractions_dir / book_id
    problems = {}
    for unit_id in unit_ids:
        why = unit_problem(base / unit_id, strict=strict)
        if why is not None:
            problems[unit_id] = why
    return problems


def completed_unit_dirs(
    conn: sqlite3.Connection, config: LoreConfig, book_id: str
) -> list[Path]:
    """The directories of the book's completed units.

    Raises DamagedUnitsError, after demoting them, when any completed unit's
    directory is missing or its parquet files are empty or unreadable.
    """
    rows = conn.execute(
        "SELECT detail FROM jobs WHERE kind = ? AND book_id = ? AND state = 'completed'"
        " ORDER BY detail",
        (JOB_KIND, book_id),
    ).fetchall()
    base = config.extractions_dir / book_id
    unit_ids: list[str] = []
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
        unit_ids.append(unit_id)
    problems = completed_units_with_problems(conn, config, book_id, unit_ids,
                                             strict=False)
    if problems:
        demote_units(conn, book_id, problems)
        raise DamagedUnitsError(book_id, problems)
    return [base / unit_id for unit_id in unit_ids]


def _read(dirs: list[Path], filename: str,
          conn: sqlite3.Connection | None) -> list[dict]:
    rows: list[dict] = []
    problems: dict[str, str] = {}
    for directory in dirs:
        path = directory / filename
        try:
            rows.extend(pq.read_table(path).to_pylist())
        except Exception as exc:  # noqa: BLE001 - missing, empty or corrupt
            problems[directory.name] = f"{filename} is unreadable: {exc}"
    if problems:
        book_id = dirs[0].parent.name
        if conn is None:
            first = sorted(problems)[0]
            raise MissingArtifactError(
                f"unit {first!r} is marked completed but its {filename} is"
                f" missing or unreadable: {problems[first]}. Re-run"
                f" `lore extract {book_id} --resume`."
            )
        demote_units(conn, book_id, problems)
        raise DamagedUnitsError(book_id, problems)
    return rows


def read_triplets(dirs: list[Path], *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Every unit's triplets. With `conn`, a unit whose file cannot be read
    is demoted (see DamagedUnitsError)."""
    return _read(dirs, "triplets.parquet", conn)


def read_embeddings(dirs: list[Path], *, conn: sqlite3.Connection | None = None) -> list[dict]:
    return _read(dirs, "embeddings.parquet", conn)
