"""traces: every question's model calls, keyed by request id

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traces",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.String(36), sa.ForeignKey("chats.id", ondelete="CASCADE")),
        sa.Column("message_id", sa.Integer,
                  sa.ForeignKey("messages.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("world", sa.String(200)),
        sa.Column("depth", sa.String(16)),
        sa.Column("decomposition_json", sa.JSON),
        sa.Column("steps_json", sa.JSON),
        sa.Column("calls_json", sa.JSON),
        sa.Column("answer_json", sa.JSON),
        sa.Column("error", sa.Text),
        sa.Column("timings_json", sa.JSON),
        sa.Column("model_usage_json", sa.JSON),
    )
    op.create_index("ix_traces_created", "traces", ["created_at"])
    op.create_index("ix_traces_user", "traces", ["user_id", "created_at"])
    op.create_index("ix_traces_chat", "traces", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_traces_chat", table_name="traces")
    op.drop_index("ix_traces_user", table_name="traces")
    op.drop_index("ix_traces_created", table_name="traces")
    op.drop_table("traces")
