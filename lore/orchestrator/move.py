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
    """Get the book's job row ready for a move to `world_id`, and claim it.

    False means there is no move to run, and nothing was changed or claimed:
    - the book is already in that world and no job is waiting; or
    - it is already there with an unfinished job that is not a move to this
      world (a stopped ordinary load): a plain Continue resumes that, and
      running the move stages over it would be the wrong stages.
    Otherwise True: the row is claimed (running, fresh heartbeat; call
    `run_book` with `claimed=True`) and carries the target world until the job
    is done (jobs.move_target):
    - Already in the world with an unfinished move to it (one that got past
      `reassign`): the job resumes where it was.
    - In another world: the move starts at `remove`, whatever an older job's
      stage says (a failed load of the book's old world must not skip it).
    The target is saved only after the claim succeeds, so a refused claim
    leaves no target behind to turn a later plain Continue into a move.
    Raises JobRefused while another runner holds the book, or when a resumed
    move is waiting for the allowance to reset.
    """
    moment = now or wall_clock()
    job = jobs.get_job(conn, book_id)
    if job is not None and jobs.is_fresh(job, moment):
        raise JobRefused("a job is already running for this book")
    current = worlds.world_of(conn, book_id)
    if current is not None and current.id == world_id:
        if (job is None or job.state == jobs.DONE
                or jobs.move_target(conn, book_id) != world_id):
            return False
        ok, why = jobs.can_continue(job, moment)
        if not ok:
            raise JobRefused(why)
    elif job is None:  # a row for Continue to find, parked at the first stage
        jobs.start_job(conn, book_id, "remove", 0, now=moment, guard=False)
        jobs.finish_stage(conn, book_id, "remove", now=moment)
    elif job.stage not in _BEFORE_REASSIGN:
        jobs.finish_stage(conn, book_id, "remove", now=moment)
    jobs.claim_job(conn, book_id, now=moment)
    # Remembered on the row, so Continue (CLI or web) reruns the move, not
    # the ordinary stages, and knows the world it is heading for.
    jobs.set_move_target(conn, book_id, world_id)
    return True
