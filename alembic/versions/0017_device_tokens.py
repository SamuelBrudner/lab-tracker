"""Add device tokens for paired phone capture."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_device_tokens"
down_revision = "0016_question_refactors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("device_token_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_device_tokens_token_hash"),
    )
    op.create_index(
        "ix_device_tokens_user_id",
        "device_tokens",
        ["user_id"],
    )

    op.create_table(
        "device_enrollments",
        sa.Column("enrollment_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("offer_token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_device_token_id",
            sa.String(length=36),
            sa.ForeignKey("device_tokens.device_token_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "offer_token_hash",
            name="uq_device_enrollments_offer_token_hash",
        ),
    )
    op.create_index(
        "ix_device_enrollments_user_id",
        "device_enrollments",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_enrollments_user_id", table_name="device_enrollments")
    op.drop_table("device_enrollments")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
