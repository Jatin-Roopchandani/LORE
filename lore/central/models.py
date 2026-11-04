"""The central tables (release spec §3 and §8a).

Portable types only (String, Integer, JSON, an explicit-UTC DateTime), so the
same Alembic migrations run on SQLite (local mode, tests) and Postgres (prod).
`users`, `jobs`, `waitlist` and `invites` exist from step 1; step 2 adds
`identities`, `sessions`, `login_tokens` and `rate_hits`; step 3 adds
`api_keys` (each user's Gemini key, AES-GCM encrypted: lore.central.keys) and
`usage` (requests per user, day and model). Chats and traces arrive with the
steps that use them.

Every bearer secret is stored as its SHA-256 only (session ids, magic-link
tokens, invite codes): a copy of the database is not a set of working
credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UTC = timezone.utc

# Job states. A job is `queued`, then `claimed` by one worker, then `done` or
# `failed`. A stale claim goes back to `queued` (see lore.central.queue).
# `paused`: the user's Gemini key is missing or was rejected; the job waits,
# not failed, and goes back to `queued` when the user saves a working key.
QUEUED = "queued"
CLAIMED = "claimed"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
ACTIVE_STATES = (QUEUED, CLAIMED)
# A book has at most one job in these states (uq_jobs_active_book).
LIVE_STATES = (QUEUED, CLAIMED, PAUSED)
_LIVE_SQL = "state IN ('queued', 'claimed', 'paused')"

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
        sa.CheckConstraint(
            "state IN ('queued', 'claimed', 'paused', 'done', 'failed')",
            name="ck_jobs_state"),
        # At most one live job per book: a second Continue finds the first
        # (a paused one too, which it wakes).
        sa.Index("uq_jobs_active_book", "user_id", "book_id", unique=True,
                 sqlite_where=sa.text(_LIVE_SQL),
                 postgresql_where=sa.text(_LIVE_SQL)),
        # At most one claimed job per user, enforced by the database: this
        # is the per-user serialisation, and it holds however many workers
        # race for the claim.
        sa.Index("uq_jobs_one_claim_per_user", "user_id", unique=True,
                 sqlite_where=sa.text("state = 'claimed'"),
                 postgresql_where=sa.text("state = 'claimed'")),
        sa.Index("ix_jobs_state_id", "state", "id"),
    )


class Waitlist(Base):
    """Invite requests (release spec §8a): pending, invited or declined."""

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
    """Sign-up codes (release spec §3). One use each; the code itself is shown
    once, when it is made, and only its SHA-256 is stored."""

    __tablename__ = "invites"

    code_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    created_by: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))
    used_by: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Identity(Base):
    """A way a user signs in: (provider, the provider's id for them)."""

    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_identity"),
        sa.CheckConstraint("provider IN ('google', 'github', 'email')",
                           name="ck_identities_provider"),
        sa.Index("ix_identities_user", "user_id"),
    )


class AuthSession(Base):
    """A signed-in browser. The cookie holds a random 256-bit token; the row's
    key is its SHA-256."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    # Slides forward on use, rewritten at most once a day (`refreshed_at`).
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                   default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(sa.String(300))

    __table_args__ = (
        sa.Index("ix_sessions_user", "user_id"),
        sa.Index("ix_sessions_expires", "expires_at"),
    )


class LoginToken(Base):
    """A magic-link token: single use, 15 minutes, stored hashed. `invite` is
    the invite code the link carries (a hash is useless for the carry, and the
    token is single use and short-lived, so the code is kept in the row)."""

    __tablename__ = "login_tokens"

    token_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    invite_hash: Mapped[str | None] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RateHit(Base):
    """One counted request for a rate limit; `key` is a hash (`kind:sha256`)."""

    __tablename__ = "rate_hits"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (sa.Index("ix_rate_hits_key_at", "key", "at"),)


class ApiKey(Base):
    """A user's Gemini key, sealed with AES-256-GCM (lore.central.keys).

    `ciphertext` and `nonce` open only with the master key `key_version`
    names and only for this `user_id` (it is the associated data). `last4` is
    the one part ever shown back. `last_error` is a fixed code, never provider
    text: `rejected` (Gemini refused the key) or `unchecked` (it could not be
    checked when it was saved).
    """

    __tablename__ = "api_keys"

    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False,
                                          default="gemini")
    ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    last4: Mapped[str] = mapped_column(sa.String(4), nullable=False)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                               default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(sa.String(32))

    __table_args__ = (
        sa.CheckConstraint("provider IN ('gemini')", name="ck_api_keys_provider"),
    )

    def __repr__(self) -> str:  # the sealed bytes are not for log lines either
        return f"ApiKey(user_id={self.user_id!r}, last4=..{self.last4})"


class Usage(Base):
    """Model requests per user, Google's (Pacific) day and model: the account
    menu's "used today" and the admin's view. The per-user budget itself is
    enforced from the ledger in the user's own lore.db, as in local mode."""

    __tablename__ = "usage"

    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(sa.String(10), primary_key=True)  # YYYY-MM-DD
    model: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    requests: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    __table_args__ = (sa.Index("ix_usage_day", "day"),)


class Chat(Base):
    """One conversation in a user's history (release step 4). The web UI shows
    one per question today; `/api/ask` with a `chat_id` appends to one.

    `client_id` is the browser's own id for a chat imported from its
    localStorage: unique per user, so an import sent twice makes one chat.
    `deleted_at` marks a soft delete: the chat is gone for the user, its
    traces are deleted at once, and the row itself is purged first when the
    user reaches MAX_CHATS.
    """

    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str | None] = mapped_column(sa.String(64))
    world: Mapped[str | None] = mapped_column(sa.String(200))
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "client_id", name="uq_chats_user_client"),
        sa.Index("ix_chats_user_updated", "user_id", "updated_at"),
    )


class Message(Base):
    """One question and the answer the UI rendered for it.

    `answer_json` is the final answer payload ({"result": SearchResult,
    "steps": [stream events]}), so a chat reopens exactly with no model call;
    past 1 MB it is a marker ({"truncated": true, "bytes": n}) instead.
    `role` is `assistant` (one row per question and its answer).
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False,
                                      default="assistant")
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    answer_json: Mapped[dict | None] = mapped_column(sa.JSON)
    depth: Mapped[str | None] = mapped_column(sa.String(16))
    world: Mapped[str | None] = mapped_column(sa.String(200))
    request_id: Mapped[str | None] = mapped_column(sa.String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    # Bytes this row stores (question + answer_json as JSON): the per-user
    # history quota adds these up (lore.central.history.MAX_USER_BYTES).
    size: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0,
                                      server_default="0")

    __table_args__ = (sa.Index("ix_messages_chat", "chat_id", "id"),)


class Trace(Base):
    """Everything one question did (release step 4), keyed by its request id:
    the decomposition, the step events the UI showed, every model call in
    order (stage, model, prompt, reply, latency, ok or error, and its HTTP
    attempts), the answer or the error, timings and requests per model.

    Built by lore.tracing; prompts and replies are capped at 64 KB and every
    text is redacted (no API key, cookie or session id can enter). Kept 30
    days (lore.central.traces.purge_expired); deleting the chat or the
    account deletes it. Readable only by admins and `lore trace`.
    """

    __tablename__ = "traces"

    request_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(
        sa.String(36), sa.ForeignKey("chats.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    world: Mapped[str | None] = mapped_column(sa.String(200))
    depth: Mapped[str | None] = mapped_column(sa.String(16))
    decomposition_json: Mapped[dict | None] = mapped_column(sa.JSON)
    steps_json: Mapped[list | dict | None] = mapped_column(sa.JSON)
    calls_json: Mapped[list | dict | None] = mapped_column(sa.JSON)
    answer_json: Mapped[dict | None] = mapped_column(sa.JSON)
    error: Mapped[str | None] = mapped_column(sa.Text)
    timings_json: Mapped[dict | None] = mapped_column(sa.JSON)
    model_usage_json: Mapped[dict | None] = mapped_column(sa.JSON)

    __table_args__ = (
        sa.Index("ix_traces_created", "created_at"),
        sa.Index("ix_traces_user", "user_id", "created_at"),
        sa.Index("ix_traces_chat", "chat_id"),
    )
