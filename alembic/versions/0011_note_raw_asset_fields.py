"""Persist raw note asset metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_note_raw_asset_fields"
down_revision = "0010_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notes") as batch_op:
        batch_op.add_column(sa.Column("raw_storage_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("raw_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("raw_content_type", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("raw_size_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("raw_checksum", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notes") as batch_op:
        batch_op.drop_column("raw_checksum")
        batch_op.drop_column("raw_size_bytes")
        batch_op.drop_column("raw_content_type")
        batch_op.drop_column("raw_filename")
        batch_op.drop_column("raw_storage_id")
