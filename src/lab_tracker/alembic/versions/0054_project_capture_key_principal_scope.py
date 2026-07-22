"""Scope project capture keys to their creating principal.

Revision ID: 0054_project_capture_key_principal_scope
Revises: 0053_personal_access_token_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_project_capture_key_principal_scope"
down_revision = "0053_personal_access_token_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _preflight_scoped_keys()
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_constraint("uq_projects_client_capture", type_="unique")
            batch_op.create_unique_constraint(
                "uq_projects_creator_client_capture",
                ["created_by", "client_capture_id"],
            )
            batch_op.create_check_constraint(
                "ck_projects_client_capture_creator",
                "client_capture_id IS NULL OR "
                "(created_by IS NOT NULL AND TRIM(created_by) <> '')",
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _preflight_global_keys()
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("projects") as batch_op:
            batch_op.drop_constraint(
                "ck_projects_client_capture_creator",
                type_="check",
            )
            batch_op.drop_constraint(
                "uq_projects_creator_client_capture",
                type_="unique",
            )
            batch_op.create_unique_constraint(
                "uq_projects_client_capture",
                ["client_capture_id"],
            )
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _preflight_scoped_keys() -> None:
    connection = op.get_bind()
    missing_creator = connection.execute(
        sa.text(
            "SELECT project_id, client_capture_id FROM projects "
            "WHERE client_capture_id IS NOT NULL "
            "AND (created_by IS NULL OR TRIM(created_by) = '') LIMIT 1"
        )
    ).first()
    if missing_creator is not None:
        raise RuntimeError(
            "Cannot scope project client_capture_id values: keyed project "
            f"{missing_creator[0]} has no created_by principal. Backfill or clear "
            "that capture key before retrying migration 0054."
        )

    duplicate = connection.execute(
        sa.text(
            "SELECT created_by, client_capture_id, COUNT(*) FROM projects "
            "WHERE client_capture_id IS NOT NULL "
            "GROUP BY created_by, client_capture_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot enforce principal-scoped project capture keys: creator "
            f"{duplicate[0]!r} has {duplicate[2]} projects with client_capture_id "
            f"{duplicate[1]!r}. Reconcile them before retrying migration 0054."
        )


def _preflight_global_keys() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT client_capture_id, COUNT(*) FROM projects "
            "WHERE client_capture_id IS NOT NULL "
            "GROUP BY client_capture_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot restore globally unique project capture keys: "
            f"client_capture_id {duplicate[0]!r} is used by {duplicate[1]} principals."
        )


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
