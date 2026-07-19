"""Add client capture idempotency keys to projects and questions.

Mirrors the notes precedent (0043) so consumer get_or_create_* helpers can pass a
deterministic idempotency key and have concurrent identical creates resolve to a
single canonical entity instead of duplicating a graph node.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052_project_question_client_capture"
down_revision = "0051_server_resident_agentic_drafting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("projects") as batch_op:
            batch_op.add_column(sa.Column("client_capture_id", sa.String(length=120)))
            batch_op.create_unique_constraint(
                "uq_projects_client_capture",
                ["client_capture_id"],
            )
        with op.batch_alter_table("questions") as batch_op:
            batch_op.add_column(sa.Column("client_capture_id", sa.String(length=120)))
            batch_op.create_unique_constraint(
                "uq_questions_project_client_capture",
                ["project_id", "client_capture_id"],
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("questions") as batch_op:
            batch_op.drop_constraint(
                "uq_questions_project_client_capture",
                type_="unique",
            )
            batch_op.drop_column("client_capture_id")
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_constraint(
                "uq_projects_client_capture",
                type_="unique",
            )
            batch_op.drop_column("client_capture_id")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
