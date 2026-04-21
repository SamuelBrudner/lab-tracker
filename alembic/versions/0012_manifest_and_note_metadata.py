"""Persist dataset manifest fields, question provenance, and note metadata."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_manifest_and_note_metadata"
down_revision = "0011_note_raw_asset_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("questions") as batch_op:
        batch_op.add_column(sa.Column("source_provenance", sa.String(length=255), nullable=True))

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.add_column(sa.Column("manifest_files", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_metadata", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_nwb_metadata", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_bids_metadata", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_note_ids", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_extraction_provenance", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("manifest_source_session_id", sa.String(length=36), nullable=True))

    with op.batch_alter_table("notes") as batch_op:
        batch_op.add_column(sa.Column("metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notes") as batch_op:
        batch_op.drop_column("metadata")

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_column("manifest_source_session_id")
        batch_op.drop_column("manifest_extraction_provenance")
        batch_op.drop_column("manifest_note_ids")
        batch_op.drop_column("manifest_bids_metadata")
        batch_op.drop_column("manifest_nwb_metadata")
        batch_op.drop_column("manifest_metadata")
        batch_op.drop_column("manifest_files")

    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_column("source_provenance")
