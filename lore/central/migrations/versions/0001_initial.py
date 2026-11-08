"""users, jobs, waitlist, invites

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_ACTIVE = sa.text("state IN ('queued', 'claimed')")
_CLAIMED = sa.text("state = 'claimed'")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(200)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("book_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("claimed_by", sa.String(128)),
        sa.Column("claimed_at", sa.DateTime()),
        sa.Column("heartbeat_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("outcome", sa.String(32)),
        sa.Column("error", sa.Text),
        sa.CheckConstraint("kind IN ('process', 'move')", name="ck_jobs_kind"),
        sa.CheckConstraint("state IN ('queued', 'claimed', 'done', 'failed')",
                           name="ck_jobs_state"),
    )
    op.create_index("uq_jobs_active_book", "jobs", ["user_id", "book_id"],
                    unique=True, sqlite_where=_ACTIVE, postgresql_where=_ACTIVE)
    op.create_index("uq_jobs_one_claim_per_user", "jobs", ["user_id"],
                    unique=True, sqlite_where=_CLAIMED, postgresql_where=_CLAIMED)
    op.create_index("ix_jobs_state_id", "jobs", ["state", "id"])
    op.create_table(
        "waitlist",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("reading", sa.Text),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("invited_at", sa.DateTime()),
    )
    op.create_table(
        "invites",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("created_by", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("used_by", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("invites")
    op.drop_table("waitlist")
    op.drop_table("jobs")
    op.drop_table("users")
