"""Default the daily-review run time to the evening (18:00).

The product advertises the daily review as an end-of-day activity, so a freshly
enabled project should prefill an evening run time. Only the column default for
future inserts changes; existing projects keep whatever time they have set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_evening_default_run_time"
down_revision = "0044_personal_access_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.alter_column(
            "run_at_local_time",
            existing_type=sa.String(length=5),
            existing_nullable=False,
            server_default="18:00",
        )


def downgrade() -> None:
    with op.batch_alter_table("graph_draft_batch_settings") as batch_op:
        batch_op.alter_column(
            "run_at_local_time",
            existing_type=sa.String(length=5),
            existing_nullable=False,
            server_default="06:00",
        )
