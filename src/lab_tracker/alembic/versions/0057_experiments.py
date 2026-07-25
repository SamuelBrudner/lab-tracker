"""Add first-class Experiments and Session/Dataset memberships."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_experiments"
down_revision = "0056_claim_confidence_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "primary_question_id",
            sa.String(length=36),
            sa.ForeignKey("questions.question_id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column(
            "origin",
            sa.String(length=32),
            nullable=False,
            server_default="user",
        ),
        sa.Column(
            "change_set_id",
            sa.String(length=36),
            sa.ForeignKey(
                "graph_change_sets.change_set_id",
                ondelete="SET NULL",
            ),
        ),
        sa.Column("origin_provider", sa.String(length=80)),
        sa.Column("origin_model", sa.String(length=255)),
        sa.Column("origin_prompt_version", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_experiments_project_created_at",
        "experiments",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_experiments_primary_question_id",
        "experiments",
        ["primary_question_id"],
    )
    op.create_index(
        "ix_experiments_created_by_user_id",
        "experiments",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_experiments_change_set_id",
        "experiments",
        ["change_set_id"],
    )

    op.create_table(
        "experiment_sessions",
        sa.Column(
            "experiment_id",
            sa.String(length=36),
            sa.ForeignKey("experiments.experiment_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_experiment_sessions_session_id",
        "experiment_sessions",
        ["session_id"],
    )

    op.create_table(
        "experiment_datasets",
        sa.Column(
            "experiment_id",
            sa.String(length=36),
            sa.ForeignKey("experiments.experiment_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dataset_id",
            sa.String(length=36),
            sa.ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_by", sa.String(length=255)),
        sa.Column(
            "created_by_user_id",
            sa.String(length=36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_experiment_datasets_dataset_id",
        "experiment_datasets",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_experiment_datasets_dataset_id",
        table_name="experiment_datasets",
    )
    op.drop_table("experiment_datasets")
    op.drop_index(
        "ix_experiment_sessions_session_id",
        table_name="experiment_sessions",
    )
    op.drop_table("experiment_sessions")
    op.drop_index("ix_experiments_change_set_id", table_name="experiments")
    op.drop_index(
        "ix_experiments_created_by_user_id",
        table_name="experiments",
    )
    op.drop_index(
        "ix_experiments_primary_question_id",
        table_name="experiments",
    )
    op.drop_index(
        "ix_experiments_project_created_at",
        table_name="experiments",
    )
    op.drop_table("experiments")
