"""Tighten nullable drift from graph and goal migrations."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039_not_null_tightening_0014_0024"
down_revision = "0038_entity_versions"
branch_labels = None
depends_on = None


_TABLE_COLUMNS: dict[str, list[tuple[str, sa.types.TypeEngine[object], str]]] = {
    "graph_change_sets": [
        ("source_note_ids", sa.JSON(), "'[]'"),
        ("context_packet", sa.JSON(), "'{}'"),
        ("summary", sa.Text(), "''"),
        ("uncertain_fields", sa.JSON(), "'[]'"),
        ("clarification_requests", sa.JSON(), "'[]'"),
        ("error_metadata", sa.JSON(), "'{}'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "graph_change_operations": [
        ("payload", sa.JSON(), "'{}'"),
        ("rationale", sa.Text(), "''"),
        ("source_refs", sa.JSON(), "'[]'"),
        ("error_metadata", sa.JSON(), "'{}'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "question_refactors": [
        ("source_snapshot", sa.JSON(), "'{}'"),
        ("replacement_snapshot", sa.JSON(), "'{}'"),
        ("relationship_changes", sa.JSON(), "'{}'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "project_memberships": [
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "goals": [
        ("summary", sa.Text(), "''"),
        ("status", sa.String(length=20), "'planned'"),
        ("attributes", sa.JSON(), "'{}'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "goal_links": [
        ("link_status", sa.String(length=20), "'candidate'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "graph_draft_batch_settings": [
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
    "graph_draft_batch_runs": [
        ("summary", sa.Text(), "''"),
        ("error_metadata", sa.JSON(), "'{}'"),
        ("created_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("updated_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
        ("started_at", sa.DateTime(timezone=True), "CURRENT_TIMESTAMP"),
    ],
}


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        for table_name, columns in _TABLE_COLUMNS.items():
            for column_name, _column_type, default_sql in columns:
                op.execute(
                    f"UPDATE {table_name} SET {column_name} = {default_sql} "
                    f"WHERE {column_name} IS NULL"
                )
        _set_nullable(False)
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        _set_nullable(True)
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")


def _set_nullable(nullable: bool) -> None:
    for table_name, columns in _TABLE_COLUMNS.items():
        with op.batch_alter_table(table_name) as batch_op:
            for column_name, column_type, _default_sql in columns:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=nullable,
                )
