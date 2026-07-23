"""Add idempotency records for atomic evidence-bundle commands.

Revision ID: 0055_evidence_bundles
Revises: 0054_project_capture_key_principal_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_evidence_bundles"
down_revision = "0054_project_capture_key_principal_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence_bundles",
        sa.Column("bundle_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "created_by",
            "idempotency_key",
            name="uq_evidence_bundles_project_creator_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("evidence_bundles")
