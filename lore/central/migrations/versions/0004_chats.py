"""chats and messages: each user's history on the server

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(64)),
        sa.Column("world", sa.String(200)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.UniqueConstraint("user_id", "client_id", name="uq_chats_user_client"),
    )
    op.create_index("ix_chats_user_updated", "chats", ["user_id", "updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.String(36),
                  sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer_json", sa.JSON),
        sa.Column("depth", sa.String(16)),
        sa.Column("world", sa.String(200)),
        sa.Column("request_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_messages_chat", "messages", ["chat_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_messages_chat", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_chats_user_updated", table_name="chats")
    op.drop_table("chats")
