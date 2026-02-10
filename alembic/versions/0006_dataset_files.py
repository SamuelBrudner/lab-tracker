"""Dataset file attachments.

Revision ID: 0006_dataset_files
Revises: 0005_projects_created_by
Create Date: 2026-02-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_dataset_files"
down_revision = "0005_projects_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_files",
        sa.Column("file_id", sa.String(length=36), primary_key=True),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("storage_id", sa.String(length=36), nullable=False),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.dataset_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("dataset_id", "path", name="uq_dataset_files_dataset_path"),
    )
    op.create_index(
        "ix_dataset_files_dataset_id",
        "dataset_files",
        ["dataset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_files_dataset_id", table_name="dataset_files")
    op.drop_table("dataset_files")

