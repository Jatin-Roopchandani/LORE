"""hardening: admin_events, in-flight slots, and invites bound to an email

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("admin_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_user_id", sa.String(64)),
        sa.Column("detail", sa.JSON),
    )
    op.create_index("ix_admin_events_at", "admin_events", ["at"])
    op.create_table(
        "inflight_slots",
        sa.Column("kind", sa.String(80), primary_key=True),
        sa.Column("slot", sa.Integer, primary_key=True),
        sa.Column("holder", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_inflight_expires", "inflight_slots", ["expires_at"])
    with op.batch_alter_table("invites") as batch:
        batch.add_column(sa.Column("email", sa.String(320), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invites") as batch:
        batch.drop_column("email")
    op.drop_index("ix_inflight_expires", table_name="inflight_slots")
    op.drop_table("inflight_slots")
    op.drop_index("ix_admin_events_at", table_name="admin_events")
    op.drop_table("admin_events")
