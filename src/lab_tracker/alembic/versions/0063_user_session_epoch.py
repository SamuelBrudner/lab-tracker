"""Add a per-user session epoch so session JWTs can be revoked."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_user_session_epoch"
down_revision = "0062_member_onboarding_purpose"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "session_epoch",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    # SQLite drops the column by rebuilding ``users`` (copy, DROP, rename).
    # With foreign keys enforced that DROP fires ON DELETE CASCADE / SET NULL
    # on every table referencing users, so disable them around the rebuild.
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("session_epoch")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    """Toggle SQLite FKs around batch table rebuilds.

    env.py configures SQLite migrations with transactional_ddl=False so this
    PRAGMA is honored before Alembic recreates the parent table.
    """
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
