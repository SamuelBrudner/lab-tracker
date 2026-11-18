"""Bind proposal email deliveries to a single operation revision and reply."""

import sqlalchemy as sa
from alembic import op

revision = "0069_proposal_email_reply"
down_revision = "0068_verified_review_mailbox"
branch_labels = None
depends_on = None

_TABLE = "review_email_outbox"
_CONSTRAINT = "ck_review_email_outbox_event_type"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            _CONSTRAINT,
            "event_type IN ('review_ready', 'test', 'proposal', 'reply_ack', 'reply_help')",
        )
        batch.add_column(sa.Column("parent_delivery_id", sa.String(36)))
        batch.add_column(sa.Column("operation_id", sa.String(36)))
        batch.add_column(sa.Column("operation_revision", sa.String(64)))
        batch.add_column(sa.Column("reply_expires_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("replied_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("reply_outcome", sa.String(30)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM review_email_outbox "
            "WHERE event_type IN ('proposal', 'reply_ack', 'reply_help')"
        )
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, "event_type IN ('review_ready', 'test')")
        for column in (
            "parent_delivery_id",
            "operation_id",
            "operation_revision",
            "reply_expires_at",
            "replied_at",
            "reply_outcome",
        ):
            batch.drop_column(column)
