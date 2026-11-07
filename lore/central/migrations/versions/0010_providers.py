"""AI providers: one key per (user, provider), the active provider, token usage

- api_keys: the primary key becomes (user_id, provider); provider may be
  gemini, anthropic or openai; new columns model, fast_model and models (the
  model ids the key's last check listed). Existing Gemini rows are copied
  unchanged (same ciphertext, nonce, version and dates).
- users: active_provider (default gemini) and daily_token_cap (null = off).
- usage: provider, input_tokens and output_tokens (Gemini rows: gemini, 0, 0).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_COLUMNS = ("user_id", "provider", "ciphertext", "nonce", "key_version", "last4",
            "added_at", "last_ok_at", "last_error")


def _key_table(name: str, providers: str, composite: bool) -> None:
    columns = [
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        sa.Column("provider", sa.String(16), primary_key=composite, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("key_version", sa.String(16), nullable=False),
        sa.Column("last4", sa.String(4), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime()),
        sa.Column("last_error", sa.String(32)),
    ]
    if composite:
        columns += [sa.Column("model", sa.String(64)), sa.Column("fast_model", sa.String(64)),
                    sa.Column("models", sa.JSON)]
    op.create_table(name, *columns,
                    # Check constraints are named per table (both dialects), so
                    # the final name can be used while the old table exists.
                    sa.CheckConstraint(f"provider IN ({providers})",
                                       name="ck_api_keys_provider"))


def upgrade() -> None:
    _key_table("api_keys_new", "'gemini', 'anthropic', 'openai'", composite=True)
    cols = ", ".join(_COLUMNS)
    op.execute(f"INSERT INTO api_keys_new ({cols}) SELECT {cols} FROM api_keys")
    op.drop_table("api_keys")
    op.rename_table("api_keys_new", "api_keys")

    # Plain ALTER TABLE ADD COLUMN on both dialects: a batch rebuild of
    # `users` on SQLite would cascade-delete every row that references it.
    op.add_column("users", sa.Column("active_provider", sa.String(16), nullable=False,
                                     server_default="gemini"))
    op.add_column("users", sa.Column("daily_token_cap", sa.Integer, nullable=True))
    with op.batch_alter_table("usage") as batch:
        batch.add_column(sa.Column("provider", sa.String(16), nullable=False,
                                   server_default="gemini"))
        batch.add_column(sa.Column("input_tokens", sa.BigInteger, nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("output_tokens", sa.BigInteger, nullable=False,
                                   server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("usage") as batch:
        batch.drop_column("output_tokens")
        batch.drop_column("input_tokens")
        batch.drop_column("provider")
    if op.get_bind().dialect.name == "sqlite":
        # Not batch mode: rebuilding `users` would cascade-delete every row
        # that references it (api_keys, jobs, chats...). SQLite 3.35+ drops a
        # plain column in place.
        op.execute("ALTER TABLE users DROP COLUMN daily_token_cap")
        op.execute("ALTER TABLE users DROP COLUMN active_provider")
    else:
        op.drop_column("users", "daily_token_cap")
        op.drop_column("users", "active_provider")
    _key_table("api_keys_old", "'gemini'", composite=False)
    cols = ", ".join(_COLUMNS)
    op.execute(f"INSERT INTO api_keys_old ({cols}) SELECT {cols} FROM api_keys"
               " WHERE provider = 'gemini'")
    op.drop_table("api_keys")
    op.rename_table("api_keys_old", "api_keys")

