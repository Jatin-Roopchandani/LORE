"""Dispatch loop: work units to Kaggle and back, resumably.

Each unit is idempotent — chunk IDs are deterministic, so re-running a
unit reproduces exactly the same work and overwrites its own artifacts.
Nothing is marked complete until its outputs validate.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lore.config import LoreConfig
from lore.ingest.registry import chunks_for_book
from lore.kaggle_api.client import KaggleClient
from lore.orchestrator import quota
from lore.orchestrator.quota import QuotaExceeded
from lore.orchestrator.templates import materialize_extract_run
from lore.orchestrator.workunit import WorkUnit, pack_units, validate_output

JOB_KIND = "extract_unit"


@dataclass
class RunReport:
    book_id: str
    units_total: int = 0
    units_completed: int = 0
    units_failed: int = 0
    chunks_ok: int = 0
    chunks_dead: int = 0
    gpu_hours: float = 0.0
    stopped_reason: str | None = None


def unit_state(conn: sqlite3.Connection, unit_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM jobs WHERE kind = ? AND detail = ?"
        " ORDER BY id DESC LIMIT 1",
        (JOB_KIND, unit_id),
    ).fetchone()
    return row[0] if row else None


def set_unit_state(
    conn: sqlite3.Connection, book_id: str, unit_id: str, state: str, error: str = ""
) -> None:
    with conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND detail = ?", (JOB_KIND, unit_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE jobs SET state = ?, error = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (state, error, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO jobs (kind, book_id, state, detail, error)"
                " VALUES (?, ?, ?, ?, ?)",
                (JOB_KIND, book_id, state, unit_id, error),
            )


def _kaggle_config_for_extraction(config: LoreConfig):
    """Point the Phase 0 client at the extraction kernel/dataset slugs."""
    return config.kaggle.model_copy(
        update={
            "kernel_slug": config.kaggle.extract_kernel_slug,
            "dataset_slug": config.kaggle.extract_dataset_slug,
        }
    )


def _estimate_hours(unit: WorkUnit) -> float:
    """Rough pre-flight estimate for the quota gate: ~1.5s of GPU per chunk
    plus a fixed session warmup. Replaced by the manifest's real number."""
    return 0.25 + len(unit.chunks) * 1.5 / 3600.0


def _run_unit(
    config: LoreConfig,
    conn: sqlite3.Connection,
    unit: WorkUnit,
    client,
    on_event: Callable[[str], None] | None,
) -> tuple[bool, float, int, int]:
    dest = config.extractions_dir / unit.book_id / unit.unit_id
    if config.extraction_provider == "ollama":
        import asyncio

        from lore.orchestrator.local import run_unit_local

        set_unit_state(conn, unit.book_id, unit.unit_id, "dispatched")
        try:
            summary = asyncio.run(run_unit_local(config, unit, dest))
        except Exception as exc:
            set_unit_state(conn, unit.book_id, unit.unit_id, "failed")
            if on_event is not None:
                on_event(f"{unit.unit_id}: failed — {exc}")
            return False, 0.0, 0, 0
        set_unit_state(conn, unit.book_id, unit.unit_id, "completed")
        return True, 0.0, summary.chunks_ok, summary.chunks_dead
    week = quota.iso_week()
    quota.check_budget(conn, _estimate_hours(unit), week)
    quota.record_dispatch(conn, unit.unit_id, _estimate_hours(unit), week)
    set_unit_state(conn, unit.book_id, unit.unit_id, "dispatched")

    work_dir = Path(tempfile.mkdtemp(prefix=f"lore-{unit.unit_id}-"))
    try:
        kernel_dir, dataset_dir = materialize_extract_run(config, unit, work_dir)
        client.push_dataset(dataset_dir)
        client.wait_dataset_ready()
        out_dir = work_dir / "out"
        client.run_and_wait(kernel_dir, out_dir, on_tick=on_event)
        summary = validate_output(unit, out_dir)
        # Copy the artifacts out BEFORE the finally clause deletes work_dir.
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("triplets.parquet", "embeddings.parquet", "manifest.json",
                     "dead_letter.jsonl"):
            source = out_dir / name
            if source.is_file():
                shutil.copy2(source, dest / name)
    except Exception as exc:  # any failure re-queues the unit
        set_unit_state(conn, unit.book_id, unit.unit_id, "failed", error=str(exc))
        if on_event is not None:
            on_event(f"{unit.unit_id}: failed — {exc}")
        return False, 0.0, 0, 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    hours = summary.gpu_seconds / 3600.0
    quota.record_actual(conn, unit.unit_id, hours)
    set_unit_state(conn, unit.book_id, unit.unit_id, "completed")
    if on_event is not None:
        on_event(f"{unit.unit_id}: {summary.chunks_ok} ok, {summary.chunks_dead} dead")
    return True, hours, summary.chunks_ok, summary.chunks_dead


def run_extraction(
    config: LoreConfig,
    conn: sqlite3.Connection,
    book_id: str,
    *,
    resume: bool = False,
    client_factory: Callable[[object], object] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> RunReport:
    chunks = chunks_for_book(conn, book_id)
    if not chunks:
        raise ValueError(f"no chunks registered for book {book_id}")

    units = pack_units(book_id, chunks, config.extraction.work_unit_size)
    report = RunReport(book_id=book_id, units_total=len(units))

    pending = [
        unit
        for unit in units
        if not (resume and unit_state(conn, unit.unit_id) == "completed")
    ]
    for unit in pending:
        set_unit_state(conn, book_id, unit.unit_id, "queued")

    if not pending:
        return report

    make_client = client_factory or (lambda cfg: KaggleClient(cfg))
    client = make_client(_kaggle_config_for_extraction(config))

    for unit in pending:
        try:
            ok, hours, chunks_ok, chunks_dead = _run_unit(
                config, conn, unit, client, on_event
            )
        except QuotaExceeded as exc:
            # Budget ran out before this unit was attempted — end the run
            # cleanly, leave the unit re-runnable, and hand back what
            # landed so far rather than crashing out of the whole call.
            report.stopped_reason = str(exc)
            if on_event is not None:
                on_event(f"{unit.unit_id}: stopped — {exc}")
            break
        report.gpu_hours += hours
        report.chunks_ok += chunks_ok
        report.chunks_dead += chunks_dead
        if ok:
            report.units_completed += 1
        else:
            report.units_failed += 1
    return report
