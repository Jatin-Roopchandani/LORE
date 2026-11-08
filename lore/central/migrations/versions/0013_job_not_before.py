"""A queued job may wait until a time: jobs.not_before

A worker that claims a job whose book still looks running (a stopped worker's
fresh book_jobs heartbeat) gives it back with not_before = when that row goes
stale, instead of finishing it as skipped. Null: claimable now.

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE: `jobs` has partial unique indexes a batch rebuild
    # would have to recreate.
    op.add_column("jobs", sa.Column("not_before", sa.DateTime(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE jobs DROP COLUMN not_before")
    else:
        op.drop_column("jobs", "not_before")
