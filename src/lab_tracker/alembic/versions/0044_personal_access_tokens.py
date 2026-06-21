"""Add personal access tokens for MCP clients."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_personal_access_tokens"
down_revision = "0043_note_client_capture_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("token_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_personal_access_tokens_token_hash",
        ),
    )
    op.create_index(
        "ix_personal_access_tokens_user_created",
        "personal_access_tokens",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_personal_access_tokens_expires_at",
        "personal_access_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_access_tokens_expires_at",
        table_name="personal_access_tokens",
    )
    op.drop_index(
        "ix_personal_access_tokens_user_created",
        table_name="personal_access_tokens",
    )
    op.drop_table("personal_access_tokens")
