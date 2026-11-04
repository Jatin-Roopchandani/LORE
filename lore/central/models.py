"""The central tables (release spec §3 and §8a).

Portable types only (String, Integer, JSON, an explicit-UTC DateTime), so the
same Alembic migrations run on SQLite (local mode, tests) and Postgres (prod).
`users`, `jobs`, `waitlist` and `invites` exist from step 1; the tables for
identities, sessions, keys, chats and traces arrive with the steps that use
them.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UTC = timezone.utc

# Job states. A job is `queued`, then `claimed` by one worker, then `done` or
# `failed`. A stale claim goes back to `queued` (see lore.central.queue).
QUEUED = "queued"
CLAIMED = "claimed"
DONE = "done"
FAILED = "failed"
ACTIVE_STATES = (QUEUED, CLAIMED)

KIND_PROCESS = "process"  # continue (or start) a book's processing
KIND_MOVE = "move"        # move a book to another world; payload: {"world": id}


class UTCDateTime(sa.TypeDecorator):
    """A timezone-aware UTC datetime on every backend.

    SQLite stores no zone, so a plain DateTime would hand back naive values.
    This stores naive UTC and returns aware UTC; a naive value going in is a
    bug, not a guess.
    """

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a timezone-aware datetime is required")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # A UUID or safe slug: becomes a directory name (lore.tenancy validates).
    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(sa.String(200))
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)

    __table_args__ = (
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )


class Job(Base):
    """One unit of work for the worker: process or move one user's book.

    Per-book progress stays in that user's own `book_jobs` row; this row says
    only who is to run it, who holds it, and whether the holder is alive.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    payload: Mapped[dict | None] = mapped_column(sa.JSON)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=QUEUED)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    claimed_by: Mapped[str | None] = mapped_column(sa.String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # How a finished job ended: the book job's state (done, waiting_quota,
    # failed); `error` carries the reason for a failure.
    outcome: Mapped[str | None] = mapped_column(sa.String(32))
    error: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint("kind IN ('process', 'move')", name="ck_jobs_kind"),
        sa.CheckConstraint("state IN ('queued', 'claimed', 'done', 'failed')",
                           name="ck_jobs_state"),
        # At most one live job per book: a second Continue finds the first.
        sa.Index("uq_jobs_active_book", "user_id", "book_id", unique=True,
                 sqlite_where=sa.text("state IN ('queued', 'claimed')"),
                 postgresql_where=sa.text("state IN ('queued', 'claimed')")),
        # At most one claimed job per user, enforced by the database: this
        # is the per-user serialisation, and it holds however many workers
        # race for the claim.
        sa.Index("uq_jobs_one_claim_per_user", "user_id", unique=True,
                 sqlite_where=sa.text("state = 'claimed'"),
                 postgresql_where=sa.text("state = 'claimed'")),
        sa.Index("ix_jobs_state_id", "state", "id"),
    )


class Waitlist(Base):
    """Invite requests (release spec §8a). Schema only until step 7."""

    __tablename__ = "waitlist"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    reading: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False,
                                        default="pending")
    invited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Invite(Base):
    """Sign-up codes (release spec §3). Schema only until step 2."""

    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    created_by: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))
    used_by: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
