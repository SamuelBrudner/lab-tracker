"""Add durable review-email delivery preferences and outbox.

Revision ID: 0057_review_email_outbox
Revises: 0056_claim_confidence_bounds
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_review_email_outbox"
down_revision = "0056_claim_confidence_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "email_notifications_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("notification_email", sa.String(length=320), nullable=True))
        batch_op.add_column(
            sa.Column(
                "notification_email_confirmed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

    op.create_table(
        "review_email_outbox",
        sa.Column("delivery_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "change_set_id",
            sa.String(length=36),
            sa.ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "recipient_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("destination_email", sa.String(length=320), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=500), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('review_ready', 'test')",
            name="ck_review_email_outbox_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'retryable', 'accepted', 'failed')",
            name="ck_review_email_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_review_email_outbox_attempt_count",
        ),
        sa.CheckConstraint(
            "TRIM(destination_email) <> ''",
            name="ck_review_email_outbox_destination_email",
        ),
        sa.CheckConstraint(
            "TRIM(idempotency_key) <> ''",
            name="ck_review_email_outbox_idempotency_key",
        ),
        sa.CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL) "
            "OR (status <> 'accepted' AND accepted_at IS NULL)",
            name="ck_review_email_outbox_accepted_at",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_review_email_outbox_idempotency_key",
        ),
    )
    op.create_index(
        "ix_review_email_outbox_claim",
        "review_email_outbox",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_review_email_outbox_change_set_id",
        "review_email_outbox",
        ["change_set_id"],
    )
    op.create_index(
        "ix_review_email_outbox_recipient_user_id",
        "review_email_outbox",
        ["recipient_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_email_outbox_recipient_user_id",
        table_name="review_email_outbox",
    )
    op.drop_index(
        "ix_review_email_outbox_change_set_id",
        table_name="review_email_outbox",
    )
    op.drop_index(
        "ix_review_email_outbox_claim",
        table_name="review_email_outbox",
    )
    op.drop_table("review_email_outbox")

    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.drop_column("notification_email_confirmed_at")
        batch_op.drop_column("notification_email")
        batch_op.drop_column("email_notifications_enabled")
