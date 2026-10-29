"""Moving a book to another world, as a resumable job.

The stages are `remove`, `reassign`, `load`, `beats` (pipeline.STAGES keeps
them in that order), run by `run_book` like any other processing. A stop
resumes at the saved stage; the order is what keeps an entity from ever
listing books of two worlds (remove before reassign, reassign before load).

`prepare_move` decides where a move starts, from the book's world and its job
row, so the same command both starts a move and continues an interrupted one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from lore import worlds
from lore.orchestrator import jobs
from lore.orchestrator.pipeline import JobRefused, wall_clock

# Stages that come before the book changes world.
_BEFORE_REASSIGN = ("remove", "reassign")


def prepare_move(conn: sqlite3.Connection, book_id: str, world_id: int, *,
                 now: datetime | None = None) -> bool:
    """Get the book's job row ready for a move to `world_id`.

    False means there is nothing to do: the book is already in that world and
    no unfinished job is waiting. Otherwise True, and `run_book` with the move
    stages runs the move.
    - Already in the world with an unfinished job: an interrupted move past
      `reassign` (or a stopped load). The job resumes where it was.
    - In another world: the move starts at `remove`, whatever an older job's
      stage says (a failed load of the book's old world must not skip it).
    The target world is saved on the job row (jobs.move_target) until the
    job is done.
    Raises JobRefused while another runner holds the book.
    """
    moment = now or wall_clock()
    job = jobs.get_job(conn, book_id)
    if job is not None and jobs.is_fresh(job, moment):
        raise JobRefused("a job is already running for this book")
    current = worlds.world_of(conn, book_id)
    if current is not None and current.id == world_id:
        return job is not None and job.state != jobs.DONE
    if job is None:  # a row for Continue to find, parked at the first stage
        jobs.start_job(conn, book_id, "remove", 0, now=moment, guard=False)
        jobs.finish_stage(conn, book_id, "remove", now=moment)
    elif job.stage not in _BEFORE_REASSIGN:
        jobs.finish_stage(conn, book_id, "remove", now=moment)
    # Remembered on the row, so Continue (CLI or web) reruns the move, not
    # the ordinary stages, and knows the world it is heading for.
    jobs.set_move_target(conn, book_id, world_id)
    return True
