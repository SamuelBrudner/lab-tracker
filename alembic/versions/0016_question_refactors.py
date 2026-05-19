"""Add question refactor supersession records."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0016_question_refactors"
down_revision = "0015_graph_context_draft_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("questions") as batch_op:
        batch_op.add_column(
            sa.Column("superseded_by_question_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("supersedes_question_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_questions_superseded_by_question_id",
            "questions",
            ["superseded_by_question_id"],
            ["question_id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_questions_supersedes_question_id",
            "questions",
            ["supersedes_question_id"],
            ["question_id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_questions_superseded_by_question_id",
        "questions",
        ["superseded_by_question_id"],
        unique=False,
    )
    op.create_index(
        "ix_questions_supersedes_question_id",
        "questions",
        ["supersedes_question_id"],
        unique=False,
    )

    op.create_table(
        "question_refactors",
        sa.Column("refactor_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_question_id", sa.String(length=36), nullable=False),
        sa.Column("replacement_question_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=True),
        sa.Column("replacement_snapshot", sa.JSON(), nullable=True),
        sa.Column("relationship_changes", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_question_id"],
            ["questions.question_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replacement_question_id"],
            ["questions.question_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("refactor_id"),
    )
    op.create_index(
        "ix_question_refactors_source_created_at",
        "question_refactors",
        ["source_question_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_question_refactors_replacement_created_at",
        "question_refactors",
        ["replacement_question_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_question_refactors_replacement_created_at",
        table_name="question_refactors",
    )
    op.drop_index("ix_question_refactors_source_created_at", table_name="question_refactors")
    op.drop_table("question_refactors")
    op.drop_index("ix_questions_supersedes_question_id", table_name="questions")
    op.drop_index("ix_questions_superseded_by_question_id", table_name="questions")

    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_constraint("fk_questions_supersedes_question_id", type_="foreignkey")
        batch_op.drop_constraint("fk_questions_superseded_by_question_id", type_="foreignkey")
        batch_op.drop_column("supersedes_question_id")
        batch_op.drop_column("superseded_by_question_id")
