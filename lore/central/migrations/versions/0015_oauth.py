"""OAuth for MCP clients: oauth_clients, oauth_grants, oauth_codes, oauth_tokens

Codes and tokens are stored as their SHA-256 only; a confidential client's
secret is sealed with the master key.

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(64), primary_key=True),
        sa.Column("info", sa.JSON, nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary, nullable=True),
        sa.Column("key_version", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "oauth_grants",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.String(64),
                  sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_oauth_grants_user", "oauth_grants", ["user_id"])
    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", sa.String(64), primary_key=True),
        sa.Column("client_id", sa.String(64),
                  sa.ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.String(64),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("redirect_uri_explicit", sa.Boolean, nullable=False),
        sa.Column("code_challenge", sa.String(128), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column("resource", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("grant_id", sa.String(32), nullable=True),
    )
    op.create_table(
        "oauth_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("grant_id", sa.String(32),
                  sa.ForeignKey("oauth_grants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_oauth_tokens_kind"),
    )
    op.create_index("ix_oauth_tokens_grant", "oauth_tokens", ["grant_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_tokens_grant", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_codes")
    op.drop_index("ix_oauth_grants_user", table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
