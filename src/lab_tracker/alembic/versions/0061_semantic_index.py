"""Add portable derivative storage for semantic graph retrieval.

Revision ID: 0061_semantic_index
Revises: 0060_acquisition_collections
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_semantic_index"
down_revision = "0060_acquisition_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("retrieval_strategy", sa.String(length=20)),
    )
    op.add_column(
        "usage_events",
        sa.Column("retrieval_fallback", sa.String(length=40)),
    )
    op.add_column("usage_events", sa.Column("semantic_duration_ms", sa.Integer()))
    op.add_column("usage_events", sa.Column("shadow_overlap_milli", sa.Integer()))
    op.create_table(
        "semantic_index_entries",
        sa.Column("entry_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_start", sa.Integer(), nullable=False),
        sa.Column("chunk_end", sa.Integer(), nullable=False),
        sa.Column("status_snapshot", sa.String(length=40)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("document_hash", sa.String(length=64), nullable=False),
        sa.Column("renderer_version", sa.String(length=80), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "entity_type",
            "entity_id",
            "config_hash",
            "chunk_index",
            name="uq_semantic_index_entry_identity",
        ),
    )
    op.create_index(
        "ix_semantic_index_entries_project_config_status",
        "semantic_index_entries",
        ["project_id", "config_hash", "status_snapshot"],
    )

    op.create_table(
        "semantic_index_jobs",
        sa.Column("job_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_generation", sa.Integer(), nullable=False),
        sa.Column("completed_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.String(length=36)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(length=80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "entity_type",
            "entity_id",
            "config_hash",
            name="uq_semantic_index_job_identity",
        ),
    )
    op.create_index(
        "ix_semantic_index_jobs_claim",
        "semantic_index_jobs",
        ["state", "retry_at", "lease_expires_at", "updated_at"],
    )
    op.create_index(
        "ix_semantic_index_jobs_project_config",
        "semantic_index_jobs",
        ["project_id", "config_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_semantic_index_jobs_project_config",
        table_name="semantic_index_jobs",
    )
    op.drop_index("ix_semantic_index_jobs_claim", table_name="semantic_index_jobs")
    op.drop_table("semantic_index_jobs")
    op.drop_index(
        "ix_semantic_index_entries_project_config_status",
        table_name="semantic_index_entries",
    )
    op.drop_table("semantic_index_entries")
    op.drop_column("usage_events", "shadow_overlap_milli")
    op.drop_column("usage_events", "semantic_duration_ms")
    op.drop_column("usage_events", "retrieval_fallback")
    op.drop_column("usage_events", "retrieval_strategy")
