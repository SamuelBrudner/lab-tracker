"""Persist acquisition outputs.

Revision ID: 0009_acquisition_outputs
Revises: 0008_project_review_policy
Create Date: 2026-04-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_acquisition_outputs"
down_revision = "0008_project_review_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_outputs",
        sa.Column("output_id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("file_path", sa.String(length=1000), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "session_id",
            "file_path",
            name="uq_acquisition_outputs_session_path",
        ),
    )
    op.create_index(
        "ix_acquisition_outputs_session_id",
        "acquisition_outputs",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_acquisition_outputs_session_id", table_name="acquisition_outputs")
    op.drop_table("acquisition_outputs")
