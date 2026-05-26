"""Persist managed visualization asset metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_visualization_assets"
down_revision = "0019_merge_daily_reviews_and_project_collaboration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("visualizations") as batch_op:
        batch_op.add_column(sa.Column("asset_storage_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("asset_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("asset_content_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("asset_size_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("asset_checksum", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("visualizations") as batch_op:
        batch_op.drop_column("asset_checksum")
        batch_op.drop_column("asset_size_bytes")
        batch_op.drop_column("asset_content_type")
        batch_op.drop_column("asset_filename")
        batch_op.drop_column("asset_storage_id")
