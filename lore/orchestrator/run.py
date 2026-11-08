"""Dispatch loop: a book's work units through the extraction tier, resumably.

Two tiers: "gemini" (the remote model `[lore.llm]` picks, one request per
chunk, embeddings local) and "ollama" (fully local).

Each unit is idempotent — chunk IDs are deterministic, so re-running a
unit reproduces exactly the same work and overwrites its own artifacts.
Nothing is marked complete until its outputs are durably on disk (lore.fs:
temp, fsync, rename) and validate. A `completed` unit whose files have since
gone missing or come back damaged is not trusted: a resume demotes and
redoes it (`heal_completed`).
"""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from lore.config import KAGGLE_REMOVED, LoreConfig
from lore.extract.runner import ExtractionHalted
from lore.graph.artifacts import completed_units_with_problems, demote_units
from lore.ingest.registry import chunks_for_book
from lore.orchestrator.local import QuotaHalted
from lore.orchestrator.workunit import WorkUnit, pack_units

JOB_KIND = "extract_unit"


@dataclass
class RunReport:
    book_id: str
    units_total: int = 0
    units_completed: int = 0
    units_failed: int = 0
    chunks_ok: int = 0
    chunks_dead: int = 0
    stopped_reason: str | None = None


def unit_state(conn: sqlite3.Connection, unit_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM jobs WHERE kind = ? AND detail = ?"
        " ORDER BY id DESC LIMIT 1",
        (JOB_KIND, unit_id),
    ).fetchone()
    return row[0] if row else None


def book_units(conn: sqlite3.Connection, book_id: str, size: int) -> list[WorkUnit]:
    """The book's extraction units: its story chunks packed `size` at a time.

    A book extracted before its sections were classified keeps the packing
    its units were made with (every chunk), so a resume never orphans units
    already paid for; the loader drops what front and back matter produced.
    """
    story = chunks_for_book(conn, book_id)
    everything = chunks_for_book(conn, book_id, story_only=False)
    if len(story) == len(everything):
        return pack_units(book_id, everything, size)
    legacy = pack_units(book_id, everything, size)
    if any(unit_state(conn, u.unit_id) is not None for u in legacy):
        return legacy
    return pack_units(book_id, story, size)


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


def heal_completed(conn: sqlite3.Connection, config: LoreConfig, book_id: str,
                   units: list[WorkUnit]) -> set[str]:
    """The ids of `units` that are completed AND whose files are whole.

    A completed unit whose directory is gone or whose files are missing,
    empty or unreadable is demoted to `failed` here, so it is extracted
    again instead of being trusted (or crashing the load).
    """
    completed = [u.unit_id for u in units if unit_state(conn, u.unit_id) == "completed"]
    problems = completed_units_with_problems(conn, config, book_id, completed)
    if problems:
        demote_units(conn, book_id, problems)
    return set(completed) - set(problems)


class _UnitHalted(Exception):
    """A unit failed because its provider cannot serve ANY unit right now.

    Internal to this module: the unit is already marked failed when this is
    raised, and `run_extraction` ends the run instead of failing every
    remaining unit the same way (each attempt would spend requests).
    `quota` marks a spent daily allowance (resumable tomorrow), as opposed to
    a rejected key or an unreachable server.
    """

    def __init__(self, reason: str, quota: bool = False) -> None:
        super().__init__(reason)
        self.quota = quota


def _run_unit_here(
    conn: sqlite3.Connection,
    unit: WorkUnit,
    execute: Callable[[], object],
    on_event: Callable[[str], None] | None,
) -> tuple[bool, int, int]:
    """One unit's job-state flow: (completed?, chunks ok, chunks dead)."""
    set_unit_state(conn, unit.book_id, unit.unit_id, "dispatched")
    try:
        summary = execute()
    except Exception as exc:
        set_unit_state(conn, unit.book_id, unit.unit_id, "failed", error=str(exc)[:4000])
        if on_event is not None:
            on_event(f"{unit.unit_id}: failed — {exc}")
        if isinstance(exc, ExtractionHalted):
            raise _UnitHalted(str(exc), quota=isinstance(exc, QuotaHalted)) from exc
        return False, 0, 0
    set_unit_state(conn, unit.book_id, unit.unit_id, "completed")
    if on_event is not None:
        on_event(f"{unit.unit_id}: {summary.chunks_ok} ok, {summary.chunks_dead} dead")
    return True, summary.chunks_ok, summary.chunks_dead


def run_extraction(
    config: LoreConfig,
    conn: sqlite3.Connection,
    book_id: str,
    *,
    resume: bool = False,
    on_event: Callable[[str], None] | None = None,
    api_key: str | None = None,
    gemini_tier_factory: Callable[[LoreConfig], object] | None = None,
) -> RunReport:
    """Run every (pending) unit of a book through the configured provider.

    `extraction_provider = "gemini"` needs a key: `api_key`, else the
    environment or ~/.config/lore/gemini.env. Without one this raises
    ValueError before any unit is queued. `gemini_tier_factory` is for tests.
    The removed "kaggle" tier is refused (ValueError) the same way.
    """
    if config.extraction_provider == "kaggle":
        raise ValueError(KAGGLE_REMOVED)
    units = book_units(conn, book_id, config.extraction.work_unit_size)
    if not units:
        raise ValueError(f"no chunks registered for book {book_id}")
    report = RunReport(book_id=book_id, units_total=len(units))

    whole = heal_completed(conn, config, book_id, units) if resume else set()
    pending = [unit for unit in units if unit.unit_id not in whole]
    tier = None
    if pending and config.extraction_provider == "gemini":
        from lore.orchestrator.local import make_gemini_tier

        # Built before any unit is queued, so a missing key refuses cleanly
        # with the jobs table untouched.
        tier = (gemini_tier_factory or (lambda cfg: make_gemini_tier(cfg, api_key)))(
            config
        )

    for unit in pending:
        set_unit_state(conn, book_id, unit.unit_id, "queued")

    if not pending:
        return report

    if tier is not None:
        # ONE event loop for every unit and the close: the shared client's
        # connection pool is bound to the loop that opened it.
        with asyncio.Runner() as loop:
            try:
                for index, unit in enumerate(pending):
                    dest = config.extractions_dir / unit.book_id / unit.unit_id
                    # Embedded in the background while this unit extracts.
                    nxt = pending[index + 1] if index + 1 < len(pending) else None
                    try:
                        ok, chunks_ok, chunks_dead = _run_unit_here(
                            conn, unit,
                            lambda unit=unit, dest=dest, nxt=nxt: loop.run(
                                tier.run(unit, dest, next_unit=nxt)),
                            on_event,
                        )
                    except _UnitHalted as exc:
                        report.units_failed += 1
                        report.stopped_reason = str(exc)
                        break
                    report.chunks_ok += chunks_ok
                    report.chunks_dead += chunks_dead
                    if ok:
                        report.units_completed += 1
                    else:
                        report.units_failed += 1
            finally:
                loop.run(tier.aclose())
        return report

    # extraction_provider = "ollama": fully local, one unit at a time.
    from lore.orchestrator import local

    for unit in pending:
        dest = config.extractions_dir / unit.book_id / unit.unit_id
        ok, chunks_ok, chunks_dead = _run_unit_here(
            conn, unit,
            lambda unit=unit, dest=dest: asyncio.run(
                local.run_unit_local(config, unit, dest)),
            on_event,
        )
        report.chunks_ok += chunks_ok
        report.chunks_dead += chunks_dead
        if ok:
            report.units_completed += 1
        else:
            report.units_failed += 1
    return report
