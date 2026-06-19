"""Add client capture idempotency keys to notes."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043_note_client_capture_id"
down_revision = "0042_usage_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("notes") as batch_op:
            batch_op.add_column(sa.Column("client_capture_id", sa.String(length=120)))
            batch_op.create_unique_constraint(
                "uq_notes_project_client_capture",
                ["project_id", "client_capture_id"],
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("notes") as batch_op:
            batch_op.drop_constraint(
                "uq_notes_project_client_capture",
                type_="unique",
            )
            batch_op.drop_column("client_capture_id")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
