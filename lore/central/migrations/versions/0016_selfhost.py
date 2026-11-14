"""Self-hosting: password sign-in and server-wide settings

`users.password_hash`: an argon2id hash (lore.auth.passwords), set only in
self-host mode; NULL for every account that signs in another way.

`server_settings`: one JSON value per key. `setup` marks the first-run admin
as created (its primary key is what makes that happen exactly once);
`provider` is the admin's AI provider for everyone (lore.central.keys), its
key sealed with the master key.

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.create_table(
        "server_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("server_settings")
    if op.get_bind().dialect.name == "sqlite":
        # Not batch mode: rebuilding `users` would cascade-delete every row
        # that references it (see 0010). SQLite 3.35+ drops a column in place.
        op.execute("ALTER TABLE users DROP COLUMN password_hash")
    else:
        op.drop_column("users", "password_hash")
