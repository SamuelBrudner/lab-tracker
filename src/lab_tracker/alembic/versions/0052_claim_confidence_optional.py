"""Make claim confidence optional (soft-deprecate).

Confidence has no objective operationalization, so it is being retired as a
required Claim field. This migration relaxes the NOT NULL constraint; existing
values are preserved and nothing new is required to set it. A later migration
may drop the column entirely.

Revision ID: 0052_claim_confidence_optional
Revises: 0051_server_resident_agentic_drafting
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_claim_confidence_optional"
down_revision = "0051_server_resident_agentic_drafting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("claims") as batch_op:
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Float(),
            nullable=True,
        )


def downgrade() -> None:
    # Backfill NULLs before restoring the NOT NULL constraint so downgrade is safe.
    op.execute("UPDATE claims SET confidence = 0 WHERE confidence IS NULL")
    with op.batch_alter_table("claims") as batch_op:
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Float(),
            nullable=False,
        )
