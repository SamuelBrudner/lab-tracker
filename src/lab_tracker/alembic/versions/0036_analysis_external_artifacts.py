"""Add external run pointers to analyses."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_analysis_external_artifacts"
down_revision = "0035_entity_origin_backlinks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(sa.Column("external_artifacts", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_column("external_artifacts")
