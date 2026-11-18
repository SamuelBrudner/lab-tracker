"""Record a person's purpose for bench work separately from related targets."""

import sqlalchemy as sa
from alembic import op

revision = "0071_work_intent"
down_revision = "0070_email_review_clarification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_intents",
        sa.Column("intent_id", sa.String(36), primary_key=True),
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.note_id", ondelete="CASCADE")),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("sessions.session_id", ondelete="CASCADE")
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "question_id",
            sa.String(36),
            sa.ForeignKey("questions.question_id", ondelete="SET NULL"),
        ),
        sa.Column("question_text", sa.Text()),
        sa.Column(
            "recorded_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column(
            "review_draft_id",
            sa.String(36),
            sa.ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint("note_id", name="uq_work_intents_note_id"),
        sa.UniqueConstraint("session_id", name="uq_work_intents_session_id"),
        sa.CheckConstraint(
            "(note_id IS NULL) <> (session_id IS NULL)", name="ck_work_intents_scope"
        ),
        sa.CheckConstraint(
            "kind IN ('question', 'exploratory', 'calibration', 'maintenance')",
            name="ck_work_intents_kind",
        ),
        sa.CheckConstraint(
            "source IN ('declared', 'confirmed_in_review')", name="ck_work_intents_source"
        ),
    )


def downgrade() -> None:
    op.drop_table("work_intents")
