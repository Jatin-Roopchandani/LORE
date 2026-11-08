"""api_keys and usage; jobs can be paused (a missing or rejected Gemini key)

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_OLD_STATES = "state IN ('queued', 'claimed', 'done', 'failed')"
_NEW_STATES = "state IN ('queued', 'claimed', 'paused', 'done', 'failed')"
_OLD_LIVE = "state IN ('queued', 'claimed')"
_NEW_LIVE = "state IN ('queued', 'claimed', 'paused')"


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.String(16), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime()),
        sa.Column("last_error", sa.String(32)),
        sa.CheckConstraint("provider IN ('gemini')", name="ck_api_keys_provider"),
    )
    op.create_table(
        "usage",
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("requests", sa.Integer, nullable=False),
    )
    op.create_index("ix_usage_day", "usage", ["day"])
    _jobs_states(_NEW_STATES, _NEW_LIVE)


def downgrade() -> None:
    op.execute("UPDATE jobs SET state = 'queued' WHERE state = 'paused'")
    _jobs_states(_OLD_STATES, _OLD_LIVE)
    op.drop_index("ix_usage_day", table_name="usage")
    op.drop_table("usage")
    op.drop_table("api_keys")


def _jobs_states(states: str, live: str) -> None:
    # SQLite rebuilds the table (batch mode); Postgres alters it in place.
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("uq_jobs_active_book")
        batch.drop_constraint("ck_jobs_state", type_="check")
        batch.create_check_constraint("ck_jobs_state", states)
        batch.create_index("uq_jobs_active_book", ["user_id", "book_id"], unique=True,
                           sqlite_where=sa.text(live), postgresql_where=sa.text(live))
