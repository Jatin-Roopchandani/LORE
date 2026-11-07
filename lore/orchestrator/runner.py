"""Run one claimed book job against one library, shared by the web app's
in-process thread (local mode) and the worker (multi mode).

The caller has already claimed the book's `book_jobs` row (jobs.claim_job, or
move.prepare_move for a move) and owns it; this runs the stages and records
how it ended on that row.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from lore.config import LoreConfig
from lore.db.sqlite import open_db
from lore.orchestrator import jobs
from lore.orchestrator.jobs import JobRefused
from lore.orchestrator.pipeline import run_book


class MarksGraphed:
    """Wraps the last stage: once it succeeds the run is complete, and the
    book is `graphed`, as `lore process` leaves it. Done before the job row
    reads `done`, so a page that sees `done` never reads a stale status."""

    def __init__(self, stage) -> None:
        self._stage = stage
        self.name = stage.name

    def total(self, ctx) -> int:
        return self._stage.total(ctx)

    def shown(self, config) -> bool:
        shown = getattr(self._stage, "shown", None)
        return True if shown is None else shown(config)

    def run(self, ctx, job_ctl) -> None:
        self._stage.run(ctx, job_ctl)
        with ctx.conn:
            ctx.conn.execute("UPDATE books SET status = 'graphed' WHERE id = ?",
                             (ctx.book_id,))


def default_stages(config: LoreConfig) -> list:
    # Imported here: lore.cli imports the web app, which imports this module.
    from lore.cli import default_stages as cli_default_stages
    return cli_default_stages(config, resume=True)


def move_stages(config: LoreConfig, world_id: int) -> list:
    from lore.cli import move_stages as cli_move_stages
    return cli_move_stages(config, world_id)


def run_claimed(config: LoreConfig, book_id: str, move_world: int | None,
                clock: Callable[[], datetime], *,
                make_stages: Callable[[LoreConfig], list] = default_stages,
                make_move_stages: Callable[[LoreConfig, int], list] = move_stages,
                ) -> jobs.BookJob | None:
    """Run the stages (a move's when `move_world` is given) on its own
    connection; returns the final job row, or None when another runner raced
    us and its row stands. Never raises for a stage or setup failure: the
    claimed row must not look running until it goes stale, so it is failed."""
    conn = open_db(config.sqlite_path)
    try:
        stages = (make_move_stages(config, move_world) if move_world is not None
                  else make_stages(config))
        if stages:
            stages = [*stages[:-1], MarksGraphed(stages[-1])]
        return run_book(conn, config, book_id, stages, clock=clock, claimed=True)
    except JobRefused:
        return None  # raced a CLI run; its row stands
    except Exception as exc:  # noqa: BLE001 - a setup failure must not kill the caller
        try:
            jobs.fail(conn, book_id, f"{type(exc).__name__}: {exc}"[:4000],
                      now=clock())
            return jobs.get_job(conn, book_id)
        except sqlite3.Error:
            return None
    finally:
        conn.close()
