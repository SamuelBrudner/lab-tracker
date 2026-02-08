"""Core entities schema.

Revision ID: 0002_core_entities
Revises: 0001_initial
Create Date: 2026-01-23 20:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_core_entities"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=150), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_table(
        "questions",
        sa.Column("question_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.String(length=40), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("created_from", sa.String(length=40), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "datasets",
        sa.Column("dataset_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("commit_hash", sa.String(length=128), nullable=False),
        sa.Column("primary_question_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_question_id"], ["questions.question_id"]),
    )
    op.create_table(
        "notes",
        sa.Column("note_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("transcribed_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("session_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("primary_question_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["primary_question_id"], ["questions.question_id"]),
    )
    op.create_table(
        "analyses",
        sa.Column("analysis_id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("method_hash", sa.String(length=255), nullable=False),
        sa.Column("code_version", sa.String(length=255), nullable=False),
        sa.Column("environment_hash", sa.String(length=255), nullable=True),
        sa.Column("executed_by", sa.String(length=255), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "question_parents",
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("parent_question_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["questions.question_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_question_id"],
            ["questions.question_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("question_id", "parent_question_id"),
    )
    op.create_table(
        "dataset_question_links",
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("question_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column(
            "outcome_status",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.dataset_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.question_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("dataset_id", "question_id"),
    )
    op.create_table(
        "note_extracted_entities",
        sa.Column("extracted_entity_id", sa.Integer(), primary_key=True),
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.note_id"], ondelete="CASCADE"),
    )
    op.create_table(
        "note_targets",
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.note_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("note_id", "entity_type", "entity_id"),
    )
    op.create_table(
        "analysis_datasets",
        sa.Column("analysis_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.analysis_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.dataset_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("analysis_id", "dataset_id"),
    )


def downgrade() -> None:
    op.drop_table("analysis_datasets")
    op.drop_table("note_targets")
    op.drop_table("note_extracted_entities")
    op.drop_table("dataset_question_links")
    op.drop_table("question_parents")
    op.drop_table("analyses")
    op.drop_table("sessions")
    op.drop_table("notes")
    op.drop_table("datasets")
    op.drop_table("questions")
    op.drop_table("users")
