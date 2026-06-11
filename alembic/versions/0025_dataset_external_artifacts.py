"""Add first-class dataset manifest external artifacts."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_dataset_external_artifacts"
down_revision = "0024_graph_draft_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.add_column(sa.Column("manifest_external_artifacts", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_column("manifest_external_artifacts")
