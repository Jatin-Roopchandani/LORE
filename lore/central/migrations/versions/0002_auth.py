"""identities, sessions, login_tokens, rate_hits; invites keyed by a hash

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "provider_user_id", name="uq_identity"),
        sa.CheckConstraint("provider IN ('google', 'github', 'email')",
                           name="ck_identities_provider"),
    )
    op.create_index("ix_identities_user", "identities", ["user_id"])
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.Column("user_agent", sa.String(300)),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires", "sessions", ["expires_at"])
    op.create_table(
        "login_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("invite_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime()),
    )
    op.create_table(
        "rate_hits",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_rate_hits_key_at", "rate_hits", ["key", "at"])
    # Invite codes become hashes. Step 1 never issued any, so the table is
    # empty; clear it anyway so no plaintext code can outlive the rename.
    op.execute("DELETE FROM invites")
    with op.batch_alter_table("invites") as batch:
        batch.alter_column("code", new_column_name="code_hash",
                           existing_type=sa.String(64))


def downgrade() -> None:
    with op.batch_alter_table("invites") as batch:
        batch.alter_column("code_hash", new_column_name="code",
                           existing_type=sa.String(64))
    op.drop_table("rate_hits")
    op.drop_table("login_tokens")
    op.drop_table("sessions")
    op.drop_table("identities")
