"""reports: a user's report of an answer, quoting its request id

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_reports_status"),
    )
    op.create_index("ix_reports_created", "reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_reports_created", table_name="reports")
    op.drop_table("reports")
