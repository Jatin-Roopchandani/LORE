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
    # The model provider the user's questions and jobs run on, and their own
    # optional daily token cap (Claude and OpenAI; None = off).
    active_provider: Mapped[str] = mapped_column(sa.String(16), nullable=False,
                                                 default="gemini", server_default="gemini")
    daily_token_cap: Mapped[int | None] = mapped_column(sa.Integer)
    # Self-host mode only (migration 0016): an argon2id hash
    # (lore.auth.passwords); NULL for an account that signs in another way.
    password_hash: Mapped[str | None] = mapped_column(sa.String(255))

    __table_args__ = (
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )

    def __repr__(self) -> str:  # never the password hash
        return f"User(id={self.id!r}, role={self.role!r}, status={self.status!r})"


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
    # A queued job is not claimed before this (migration 0013): a worker gave
    # it back because its book still looked running elsewhere. Null: now.
    not_before: Mapped[datetime | None] = mapped_column(UTCDateTime)

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
    # Release step 6: an invite made by approving a waitlist entry works only
    # for that (normalized) email; NULL is a hand-out code anyone may use once.
    email: Mapped[str | None] = mapped_column(sa.String(320))


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
    """A user's model-provider key, sealed with AES-256-GCM (lore.central.keys).

    One row per (user, provider): gemini, anthropic or openai. `ciphertext` and
    `nonce` open only with the master key `key_version` names and only for
    this user and provider (they are the associated data). `last4` is the one
    part ever shown back. `last_error` is a fixed code, never provider text:
    `rejected` (the provider refused the key) or `unchecked` (it could not be
    checked when it was saved). `model` / `fast_model` are the user's choice
    (Claude, OpenAI; unused for Gemini); `models` the ids the key's last check
    listed.
    """

    __tablename__ = "api_keys"

    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(sa.String(16), primary_key=True,
                                          default="gemini")
    ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    last4: Mapped[str] = mapped_column(sa.String(4), nullable=False)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                               default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(sa.String(32))
    model: Mapped[str | None] = mapped_column(sa.String(64))
    fast_model: Mapped[str | None] = mapped_column(sa.String(64))
    models: Mapped[list | None] = mapped_column(sa.JSON)
    # Gemini only: the user says this key is on a paid plan (migration 0011):
    # no free-tier request cap or answer reserve; tokens and cost are shown.
    paid_tier: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False,
                                            server_default=sa.false())

    __table_args__ = (
        sa.CheckConstraint("provider IN ('gemini', 'anthropic', 'openai')",
                           name="ck_api_keys_provider"),
    )

    def __repr__(self) -> str:  # the sealed bytes are not for log lines either
        return f"ApiKey(user_id={self.user_id!r}, {self.provider}, last4=..{self.last4})"


class Usage(Base):
    """Model use per user, Google's (Pacific) day and model: the account
    menu's "used today", the token totals and cost estimate for Claude and
    OpenAI, the optional daily token cap, and the admin's view. The Gemini
    budget itself is enforced from the ledger in the user's own lore.db."""

    __tablename__ = "usage"

    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(sa.String(10), primary_key=True)  # YYYY-MM-DD
    model: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    requests: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="gemini",
                                          server_default="gemini")
    input_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0,
                                              server_default="0")
    output_tokens: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0,
                                               server_default="0")
    # Gemini 429s ("out of allowance") Google answered today (migration 0011):
    # the paid-plan hint needs to know there were none.
    quota_refusals: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0,
                                                server_default="0")
    # Gemini 402s ("prepaid credit used up") that day (migration 0012): only a
    # paid plan has prepaid credit, so one is a signal for the same hint.
    credit_refusals: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0,
                                                 server_default="0")

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
    # A follow-up's question rewritten to stand alone (lore.query.decompose
    # FOLLOWUPS), when it differed from the question as typed; else None.
    standalone: Mapped[str | None] = mapped_column(sa.Text)

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


class Report(Base):
    """A user's report of an answer ("this answer was wrong"), quoting the
    request id of the question, so an admin can open its trace. `status` is
    `open` or `resolved`. Kept when the trace expires (the note still says
    what went wrong); deleted with the account."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    note: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="open")

    __table_args__ = (
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_reports_status"),
        sa.Index("ix_reports_created", "created_at"),
    )


class AdminEvent(Base):
    """Every admin action (release step 6): who did what to whom, and when.

    `target_user_id` is a plain id, not a foreign key: the log outlives an
    account deleted by that very action. `detail` holds no secrets and no
    email addresses.
    """

    __tablename__ = "admin_events"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    admin_id: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    target_user_id: Mapped[str | None] = mapped_column(sa.String(64))
    detail: Mapped[dict | None] = mapped_column(sa.JSON)

    __table_args__ = (sa.Index("ix_admin_events_at", "at"),)


class ServerSetting(Base):
    """One server-wide setting (migration 0016): `setup` (the first-run admin
    was created; the primary key makes that happen once) and `provider` (the
    self-host admin's AI provider for everyone, its key sealed)."""

    __tablename__ = "server_settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="SET NULL"))

    def __repr__(self) -> str:  # a value can hold a sealed key
        return f"ServerSetting(key={self.key!r})"


class McpToken(Base):
    """A personal access token for the hosted MCP endpoint (lore.central.mcp_tokens).

    The token itself (`lore_` + 256 random bits) is shown once, when it is
    made; only its SHA-256 is stored, with `prefix` (its first characters) to
    tell tokens apart in a list. A revoked token keeps its row (`revoked_at`)
    so a lookup of it fails the same way as an unknown one.
    """

    __tablename__ = "mcp_tokens"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (sa.Index("ix_mcp_tokens_user", "user_id"),)

    def __repr__(self) -> str:  # never the hash either
        return f"McpToken(id={self.id!r}, user_id={self.user_id!r}, prefix={self.prefix!r})"


class OAuthClient(Base):
    """An MCP client registered through OAuth dynamic client registration
    (lore.mcp.oauth). `info` is its registered metadata (redirect URIs, name,
    grant types), never its secret. A confidential client's secret is sealed
    with the master key (AES-GCM, bound to the client id), because the token
    endpoint must compare it; a public client has none."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    info: Mapped[dict] = mapped_column(sa.JSON, nullable=False)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    secret_nonce: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    key_version: Mapped[str | None] = mapped_column(sa.String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)

    def __repr__(self) -> str:
        return f"OAuthClient(client_id={self.client_id!r})"


class OAuthGrant(Base):
    """One user's consent to one client: what Settings lists as a connected
    app. Revoking it ends every token issued under it."""

    __tablename__ = "oauth_grants"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    client_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False)
    client_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    scopes: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (sa.Index("ix_oauth_grants_user", "user_id"),)


class OAuthCode(Base):
    """An authorization code: single use, minutes long, stored hashed. A
    second use revokes the grant the first one made (`grant_id`)."""

    __tablename__ = "oauth_codes"

    code_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False)
    user_id: Mapped[str] = mapped_column(
        sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    redirect_uri_explicit: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    code_challenge: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    scopes: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    resource: Mapped[str | None] = mapped_column(sa.String(512))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    grant_id: Mapped[str | None] = mapped_column(sa.String(32))


class OAuthToken(Base):
    """An access token (an hour) or a refresh token (30 days, rotated on use),
    stored as its SHA-256 only."""

    __tablename__ = "oauth_tokens"

    token_hash: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False,
                                                 default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_oauth_tokens_kind"),
        sa.Index("ix_oauth_tokens_grant", "grant_id"),
    )


class InflightSlot(Base):
    """One unit of work in flight (release step 6): a question being answered,
    an upload being parsed. A limit of N for `kind` is N possible rows
    (`slot` 0..N-1); the primary key makes taking a slot atomic on every
    database and across web processes. `expires_at` is a lease, so a process
    that died frees its slots (lore.web.throttle)."""

    __tablename__ = "inflight_slots"

    kind: Mapped[str] = mapped_column(sa.String(80), primary_key=True)
    slot: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    holder: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(sa.String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (sa.Index("ix_inflight_expires", "expires_at"),)
