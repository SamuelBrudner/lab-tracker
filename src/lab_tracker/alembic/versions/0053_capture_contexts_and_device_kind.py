"""Add capture contexts and device token kind.

Revision ID: 0053_capture_contexts_and_device_kind
Revises: 0052_claim_confidence_optional
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_capture_contexts_and_device_kind"
down_revision = "0052_claim_confidence_optional"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capture_contexts",
        sa.Column("capture_context_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("site_label", sa.String(length=150)),
        sa.Column("place_label", sa.String(length=150)),
        sa.Column("default_hint", sa.String(length=500)),
        sa.Column("default_targets", sa.JSON()),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_capture_contexts_project_created_at",
        "capture_contexts",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_capture_contexts_project_revoked",
        "capture_contexts",
        ["project_id", "revoked_at"],
    )
    op.create_index(
        "ix_capture_contexts_created_by_user_id",
        "capture_contexts",
        ["created_by_user_id"],
    )
    with op.batch_alter_table("device_tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(length=20),
                nullable=False,
                server_default="mobile",
            )
        )
    op.create_index(
        "ix_device_tokens_user_kind",
        "device_tokens",
        ["user_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_tokens_user_kind", table_name="device_tokens")
    with op.batch_alter_table("device_tokens") as batch_op:
        batch_op.drop_column("kind")
    op.drop_index("ix_capture_contexts_created_by_user_id", table_name="capture_contexts")
    op.drop_index("ix_capture_contexts_project_revoked", table_name="capture_contexts")
    op.drop_index("ix_capture_contexts_project_created_at", table_name="capture_contexts")
    op.drop_table("capture_contexts")
