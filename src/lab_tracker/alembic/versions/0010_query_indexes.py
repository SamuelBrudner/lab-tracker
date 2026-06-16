"""Add query path indexes.

Revision ID: 0010_query_indexes
Revises: 0009_acquisition_outputs
Create Date: 2026-04-20 00:00:01.000000
"""

from __future__ import annotations

from alembic import op

revision = "0010_query_indexes"
down_revision = "0009_acquisition_outputs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_questions_project_created_at", "questions", ["project_id", "created_at"])
    op.create_index("ix_datasets_project_created_at", "datasets", ["project_id", "created_at"])
    op.create_index(
        "ix_dataset_reviews_dataset_requested_at",
        "dataset_reviews",
        ["dataset_id", "requested_at"],
    )
    op.create_index(
        "ix_dataset_reviews_reviewer_status_requested_at",
        "dataset_reviews",
        ["reviewer_user_id", "status", "requested_at"],
    )
    op.create_index("ix_notes_project_created_at", "notes", ["project_id", "created_at"])
    op.create_index(
        "ix_note_targets_entity_lookup",
        "note_targets",
        ["entity_type", "entity_id", "note_id"],
    )
    op.create_index("ix_sessions_project_started_at", "sessions", ["project_id", "started_at"])
    op.create_index("ix_analysis_datasets_dataset_id", "analysis_datasets", ["dataset_id"])
    op.create_index("ix_analyses_project_created_at", "analyses", ["project_id", "created_at"])
    op.create_index("ix_claim_datasets_dataset_id", "claim_datasets", ["dataset_id"])
    op.create_index("ix_claim_analyses_analysis_id", "claim_analyses", ["analysis_id"])
    op.create_index("ix_claims_project_created_at", "claims", ["project_id", "created_at"])
    op.create_index(
        "ix_dataset_question_links_question_id",
        "dataset_question_links",
        ["question_id"],
    )
    op.create_index(
        "ix_visualizations_analysis_created_at",
        "visualizations",
        ["analysis_id", "created_at"],
    )
    op.create_index("ix_visualization_claims_claim_id", "visualization_claims", ["claim_id"])


def downgrade() -> None:
    op.drop_index("ix_visualization_claims_claim_id", table_name="visualization_claims")
    op.drop_index("ix_visualizations_analysis_created_at", table_name="visualizations")
    op.drop_index("ix_dataset_question_links_question_id", table_name="dataset_question_links")
    op.drop_index("ix_claims_project_created_at", table_name="claims")
    op.drop_index("ix_claim_analyses_analysis_id", table_name="claim_analyses")
    op.drop_index("ix_claim_datasets_dataset_id", table_name="claim_datasets")
    op.drop_index("ix_analyses_project_created_at", table_name="analyses")
    op.drop_index("ix_analysis_datasets_dataset_id", table_name="analysis_datasets")
    op.drop_index("ix_sessions_project_started_at", table_name="sessions")
    op.drop_index("ix_note_targets_entity_lookup", table_name="note_targets")
    op.drop_index("ix_notes_project_created_at", table_name="notes")
    op.drop_index(
        "ix_dataset_reviews_reviewer_status_requested_at",
        table_name="dataset_reviews",
    )
    op.drop_index("ix_dataset_reviews_dataset_requested_at", table_name="dataset_reviews")
    op.drop_index("ix_datasets_project_created_at", table_name="datasets")
    op.drop_index("ix_questions_project_created_at", table_name="questions")
