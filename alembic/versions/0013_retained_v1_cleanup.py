"""Drop retired review and AI-derived schema surfaces."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_retained_v1_cleanup"
down_revision = "0012_manifest_and_note_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("note_tag_suggestions")
    op.drop_table("note_extracted_entities")
    op.drop_table("dataset_reviews")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("review_policy")

    with op.batch_alter_table("questions") as batch_op:
        batch_op.drop_column("source_provenance")
        batch_op.drop_column("created_from")

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_column("manifest_extraction_provenance")


def downgrade() -> None:
    with op.batch_alter_table("datasets") as batch_op:
        batch_op.add_column(
            sa.Column("manifest_extraction_provenance", sa.JSON(), nullable=True)
        )

    with op.batch_alter_table("questions") as batch_op:
        batch_op.add_column(sa.Column("created_from", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("source_provenance", sa.String(length=255), nullable=True))

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("review_policy", sa.String(length=20), nullable=True))

    op.create_table(
        "dataset_reviews",
        sa.Column("review_id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("reviewer_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.dataset_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_id"),
    )
    op.create_index(
        "ix_dataset_reviews_dataset_requested_at",
        "dataset_reviews",
        ["dataset_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ix_dataset_reviews_reviewer_status_requested_at",
        "dataset_reviews",
        ["reviewer_user_id", "status", "requested_at"],
        unique=False,
    )

    op.create_table(
        "note_extracted_entities",
        sa.Column("extracted_entity_id", sa.Integer(), nullable=False),
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.note_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("extracted_entity_id"),
    )

    op.create_table(
        "note_tag_suggestions",
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("entity_label", sa.String(length=255), nullable=False),
        sa.Column("vocabulary", sa.String(length=40), nullable=False),
        sa.Column("term_id", sa.String(length=255), nullable=False),
        sa.Column("term_label", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["note_id"], ["notes.note_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suggestion_id"),
        sa.UniqueConstraint(
            "note_id",
            "entity_label",
            "vocabulary",
            "term_id",
            name="uq_note_tag_suggestion",
        ),
    )
