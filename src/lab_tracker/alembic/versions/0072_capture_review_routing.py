"""Keep reviewer assignments independent of original capture authorship."""

import sqlalchemy as sa
from alembic import op

revision = "0072_capture_review_routing"
down_revision = "0071_work_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("machine_captured", sa.Boolean()))
    op.add_column("notes", sa.Column("capture_source", sa.String(80)))
    op.add_column("notes", sa.Column("review_session_id", sa.String(36)))
    op.add_column("notes", sa.Column("review_session_linked_at", sa.DateTime(timezone=True)))
    op.create_table(
        "review_operator_assignments",
        sa.Column("assignment_id", sa.String(36), primary_key=True),
        sa.Column("note_id", sa.String(36), sa.ForeignKey("notes.note_id", ondelete="CASCADE")),
        sa.Column(
            "session_id", sa.String(36), sa.ForeignKey("sessions.session_id", ondelete="CASCADE")
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.user_id", ondelete="SET NULL")),
        sa.Column(
            "assigned_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("note_id", name="uq_review_operators_note"),
        sa.UniqueConstraint("session_id", name="uq_review_operators_session"),
        sa.CheckConstraint(
            "(note_id IS NULL) <> (session_id IS NULL)", name="ck_review_operators_scope"
        ),
    )


def downgrade() -> None:
    op.drop_table("review_operator_assignments")
    op.drop_column("notes", "review_session_linked_at")
    op.drop_column("notes", "review_session_id")
    op.drop_column("notes", "capture_source")
    op.drop_column("notes", "machine_captured")
