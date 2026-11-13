"""The job queue on the central `jobs` table.

Producers (the web app) `enqueue`; workers `claim_next`, `heartbeat` while they
run, then `finish`. A worker that dies stops heartbeating, and any worker's
`requeue_stale` puts the job back.

Claim semantics, and why they hold on both databases:

- A claim is "pick a queued job, then flip it to claimed only if it is STILL
  queued", one guarded `UPDATE ... WHERE id = :id AND state = 'queued'
  RETURNING id`. Whoever's update changes the row wins; any other worker's
  update changes nothing and moves to the next candidate.
- Postgres: the candidate is selected `FOR UPDATE SKIP LOCKED`, so concurrent
  workers do not even queue on the same row, they take different ones.
- SQLite: no row locks, but every transaction begins `BEGIN IMMEDIATE`
  (lore.central.db), so claims are serialised by the database's single
  writer lock and a busy timeout; the guarded update is then trivially atomic.
- One job per user at a time: the candidate query skips users that already
  hold a claimed job, and a partial unique index (`uq_jobs_one_claim_per_user`)
  makes the database refuse a second claim for a user even if two workers
  raced past that check (possible on Postgres, where each sees only committed
  rows). The loser's update raises IntegrityError, rolls back, and tries again.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from lore.central.models import (
    CLAIMED, DONE, FAILED, KIND_MOVE, KIND_PROCESS, LIVE_STATES, PAUSED, QUEUED,
    Job, User, utcnow,
)

KINDS = (KIND_PROCESS, KIND_MOVE)
# A claim with no heartbeat for this long is abandoned. Longer than the
# per-book row's own staleness (jobs.STALE_AFTER, 120 s), so by the time a job
# is requeued its book_jobs row is stale too and the next run can claim it.
STALE_AFTER = timedelta(seconds=180)
MAX_ATTEMPTS = 3
_CLAIM_TRIES = 8


class UnknownUser(LookupError):
    """No central `users` row for this id."""


def enqueue(session: Session, user_id: str, book_id: str, kind: str,
            payload: dict | None = None) -> tuple[Job, bool]:
    """Queue a job; returns (job, created).

    A book has at most one live (queued, claimed or paused) job, so a second
    request for the same book finds the first and `created` is False. A
    paused one is woken (queued again): asking again is the user's resume.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    if session.get(User, user_id) is None:
        raise UnknownUser(user_id)
    for _ in range(2):  # the second try covers the live job finishing in between
        job = Job(user_id=user_id, book_id=book_id, kind=kind, payload=payload)
        session.add(job)
        try:
            session.commit()
            return job, True
        except IntegrityError:
            session.rollback()
            live = session.scalars(sa.select(Job).where(
                Job.user_id == user_id, Job.book_id == book_id,
                Job.state.in_(LIVE_STATES))).first()
            if live is not None:
                if live.state == PAUSED:
                    live.state = QUEUED
                    session.commit()
                return live, False
    raise RuntimeError("could not enqueue the job")


def _candidate(session: Session, now: datetime) -> Job | None:
    holder = aliased(Job)
    busy = sa.exists().where(holder.user_id == Job.user_id, holder.state == CLAIMED)
    # Only an active user's jobs run: a disabled or deleting account's wait.
    active = sa.select(User.id).where(User.status == "active")
    due = sa.or_(Job.not_before.is_(None), Job.not_before <= now)
    stmt = (sa.select(Job).where(Job.state == QUEUED, due, ~busy,
                                 Job.user_id.in_(active))
            .order_by(Job.id).limit(1)
            .with_for_update(skip_locked=True))  # ignored by SQLite
    return session.scalars(stmt).first()


def claim_next(session: Session, worker_id: str, *,
               now: datetime | None = None) -> Job | None:
    """Atomically claim the oldest queued job whose user has none running."""
    moment = now or utcnow()
    for _ in range(_CLAIM_TRIES):
        job = _candidate(session, moment)
        if job is None:
            session.rollback()
            return None
        try:
            won = session.execute(
                sa.update(Job)
                .where(Job.id == job.id, Job.state == QUEUED)
                .values(state=CLAIMED, claimed_by=worker_id, claimed_at=moment,
                        heartbeat_at=moment, attempts=Job.attempts + 1,
                        not_before=None)
                .returning(Job.id)
                .execution_options(synchronize_session=False)).first()
            session.commit()
        except IntegrityError:  # another worker took this user's turn first
            session.rollback()
            continue
        if won is not None:
            session.refresh(job)
            return job
    return None


def heartbeat(session: Session, job_id: int, worker_id: str, *,
              now: datetime | None = None) -> bool:
    """Stamp a held job alive. False when the claim is no longer this
    worker's (it was requeued as stale)."""
    res = session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.state == CLAIMED, Job.claimed_by == worker_id)
        .values(heartbeat_at=now or utcnow())
        .execution_options(synchronize_session=False))
    session.commit()
    return res.rowcount == 1


def finish(session: Session, job_id: int, worker_id: str, *, outcome: str,
           failed: bool = False, error: str | None = None,
           now: datetime | None = None) -> bool:
    """Close a held job as done (or failed). False when the claim was lost."""
    res = session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.state == CLAIMED, Job.claimed_by == worker_id)
        .values(state=FAILED if failed else DONE, outcome=outcome,
                error=(error or None) and error[:4000],
                finished_at=now or utcnow())
        .execution_options(synchronize_session=False))
    session.commit()
    return res.rowcount == 1


def requeue(session: Session, job_id: int, worker_id: str, *,
            not_before: datetime | None = None) -> bool:
    """The holder gives a claimed job back to the queue (it stopped at a safe
    point and should run again, e.g. on the user's new provider, or its book
    still looks running elsewhere: then `not_before` delays the next claim).
    Not an attempt that failed: `attempts` is given back too. Commits; False
    when the claim was lost."""
    res = session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.state == CLAIMED, Job.claimed_by == worker_id)
        .values(state=QUEUED, claimed_by=None, claimed_at=None, heartbeat_at=None,
                not_before=not_before,
                attempts=sa.case((Job.attempts > 0, Job.attempts - 1), else_=0))
        .execution_options(synchronize_session=False))
    session.commit()
    session.expire_all()
    return res.rowcount == 1


def requeue_stale(session: Session, *, now: datetime | None = None,
                  max_age: timedelta = STALE_AFTER,
                  max_attempts: int = MAX_ATTEMPTS) -> list[int]:
    """Claims with no heartbeat for `max_age` go back to queued (or fail once
    they have been tried `max_attempts` times). Returns the ids touched.

    Each update re-checks the state and the cutoff, so two workers reaping at
    once cannot both requeue a job, and a job whose heartbeat just landed is
    left alone.
    """
    moment = now or utcnow()
    cutoff = moment - max_age
    stale = session.execute(sa.select(Job.id, Job.attempts).where(
        Job.state == CLAIMED, Job.heartbeat_at < cutoff).order_by(Job.id)).all()
    touched: list[int] = []
    for job_id, attempts in stale:
        give_up = attempts >= max_attempts
        values = (dict(state=FAILED, finished_at=moment, outcome="abandoned",
                       error=f"no heartbeat after {attempts} attempts")
                  if give_up else
                  dict(state=QUEUED, claimed_by=None, claimed_at=None,
                       heartbeat_at=None))
        res = session.execute(
            sa.update(Job)
            .where(Job.id == job_id, Job.state == CLAIMED,
                   Job.heartbeat_at < cutoff)
            .values(**values).execution_options(synchronize_session=False))
        session.commit()
        if res.rowcount == 1:
            touched.append(job_id)
    return touched


def pause(session: Session, job_id: int, worker_id: str, *, outcome: str,
          error: str | None = None) -> bool:
    """Park a held job as paused (its user's key is missing or was refused).
    False when the claim was lost."""
    res = session.execute(
        sa.update(Job)
        .where(Job.id == job_id, Job.state == CLAIMED, Job.claimed_by == worker_id)
        .values(state=PAUSED, outcome=outcome, error=(error or None) and error[:4000],
                claimed_by=None, claimed_at=None, heartbeat_at=None)
        .execution_options(synchronize_session=False))
    session.commit()
    return res.rowcount == 1


def pause_user(session: Session, user_id: str, outcome: str = "key_invalid") -> list[int]:
    """Every queued job of this user becomes paused (a claimed one finishes
    on its own and parks itself). Commits; returns the ids. `outcome` says
    why: key_invalid (the default) or user_disabled (release step 6)."""
    ids = list(session.scalars(sa.select(Job.id).where(
        Job.user_id == user_id, Job.state == QUEUED)))
    if ids:
        session.execute(
            sa.update(Job).where(Job.id.in_(ids), Job.state == QUEUED)
            .values(state=PAUSED, outcome=outcome)
            .execution_options(synchronize_session=False))
    session.commit()
    session.expire_all()  # loaded Job objects see the new state
    return ids


def resume_user(session: Session, user_id: str) -> list[int]:
    """This user's paused jobs go back to queued. Commits; returns the ids."""
    ids = list(session.scalars(sa.select(Job.id).where(
        Job.user_id == user_id, Job.state == PAUSED)))
    if ids:
        session.execute(
            sa.update(Job).where(Job.id.in_(ids), Job.state == PAUSED)
            .values(state=QUEUED, outcome=None, error=None)
            .execution_options(synchronize_session=False))
    session.commit()
    session.expire_all()
    return ids


def running_books(session: Session, user_id: str) -> set[str]:
    """The user's books with a queued or claimed job: about to run, or
    running. (A paused job waits on the user, not on a worker.)"""
    return set(session.scalars(sa.select(Job.book_id).where(
        Job.user_id == user_id, Job.state.in_((QUEUED, CLAIMED)))))


def close_for_deleted_book(session: Session, user_id: str, book_id: str, *,
                           now: datetime | None = None) -> list[int]:
    """The book was deleted: its waiting (queued or paused) jobs are closed
    as failed, so nothing wakes one for a book that is gone. A claimed job
    is left to its worker (it finds no book). Commits; returns the ids."""
    ids = list(session.scalars(sa.select(Job.id).where(
        Job.user_id == user_id, Job.book_id == book_id,
        Job.state.in_((QUEUED, PAUSED)))))
    if ids:
        session.execute(
            sa.update(Job).where(Job.id.in_(ids), Job.state.in_((QUEUED, PAUSED)))
            .values(state=FAILED, outcome="book_deleted", error="the book was deleted",
                    finished_at=now or utcnow())
            .execution_options(synchronize_session=False))
    session.commit()
    session.expire_all()
    return ids
