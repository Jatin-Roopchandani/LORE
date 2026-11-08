"""Paid Gemini plans: a per-key switch, and Google's 429s counted per day

- api_keys.paid_tier (default false): the user says this Gemini key is on a
  paid plan, so LORE's free-tier daily request cap and the answer reserve do
  not apply, and tokens and an estimated cost are shown instead.
- usage.quota_refusals (default 0): the 429s Google answered that day, so the
  "your key seems to be on a paid plan" hint is shown only when there were none.

Existing rows keep today's behaviour exactly: every key is free tier.

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE ADD COLUMN on both dialects (no SQLite batch rebuild).
    op.add_column("api_keys", sa.Column("paid_tier", sa.Boolean, nullable=False,
                                        server_default=sa.false()))
    op.add_column("usage", sa.Column("quota_refusals", sa.Integer, nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 3.35+ drops a plain column in place; a batch rebuild is not needed.
        op.execute("ALTER TABLE usage DROP COLUMN quota_refusals")
        op.execute("ALTER TABLE api_keys DROP COLUMN paid_tier")
    else:
        op.drop_column("usage", "quota_refusals")
        op.drop_column("api_keys", "paid_tier")
