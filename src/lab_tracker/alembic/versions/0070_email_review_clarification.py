"""Allow verified reviewers to answer a standalone clarification by email."""

import sqlalchemy as sa
from alembic import op

revision = "0070_email_review_clarification"
down_revision = "0069_proposal_email_reply"
branch_labels = None
depends_on = None

_TABLE = "review_email_outbox"
_CONSTRAINT = "ck_review_email_outbox_event_type"
_PREVIOUS_EVENTS = "event_type IN ('review_ready', 'test', 'proposal', 'reply_ack', 'reply_help')"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(
            _CONSTRAINT,
            "event_type IN ('review_ready', 'test', 'proposal', 'reply_ack', 'reply_help', "
            "'clarification')",
        )
        batch.add_column(sa.Column("clarification_revision", sa.String(64)))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM review_email_outbox WHERE parent_delivery_id IN "
            "(SELECT delivery_id FROM review_email_outbox WHERE event_type = 'clarification')"
        )
    )
    op.execute(sa.text("DELETE FROM review_email_outbox WHERE event_type = 'clarification'"))
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, _PREVIOUS_EVENTS)
        batch.drop_column("clarification_revision")
