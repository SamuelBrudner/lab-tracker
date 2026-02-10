"""Entity tag suggestion mappings.

Revision ID: 0003_entity_tag_suggestions
Revises: 0002_core_entities
Create Date: 2026-01-29 20:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_entity_tag_suggestions"
down_revision = "0002_core_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_tag_suggestions",
        sa.Column("suggestion_id", sa.String(length=36), primary_key=True),
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("entity_label", sa.String(length=255), nullable=False),
        sa.Column("vocabulary", sa.String(length=40), nullable=False),
        sa.Column("term_id", sa.String(length=255), nullable=False),
        sa.Column("term_label", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="staged"),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["note_id"], ["notes.note_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "note_id",
            "entity_label",
            "vocabulary",
            "term_id",
            name="uq_note_tag_suggestion",
        ),
    )


def downgrade() -> None:
    op.drop_table("note_tag_suggestions")
