"""Gemini's 402 (prepaid credit used up) counted per user, day and model

- usage.credit_refusals (default 0): the 402s Google answered that day. Only a
  key on a paid plan has prepaid credit, so one is a signal for the "your key
  seems to be on a paid plan" hint.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage", sa.Column("credit_refusals", sa.Integer, nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE usage DROP COLUMN credit_refusals")
    else:
        op.drop_column("usage", "credit_refusals")
