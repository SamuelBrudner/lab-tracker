"""Record curation provenance: how operations were accepted and why notes were archived.

Adds durable, queryable curation state so the committed graph stays honest about
its own provenance:

- ``graph_change_operations`` gains ``acceptance_mode`` (human_selected /
  bulk_accepted / auto_accepted) plus the accepting actor and timestamp, so a
  rubber-stamped batch is never indistinguishable from a per-operation human
  accept.
- ``notes`` gains ``archived_reason`` and the archiving actor/timestamp, so a
  capture set aside is never silently dropped; archiving names a reason
  (including ``archived_unreviewed``).

All columns are nullable: existing rows keep NULL, meaning "not recorded" rather
than asserting a specific curation history retroactively.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046_curation_provenance"
down_revision = "0045_evening_default_run_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("graph_change_operations") as batch_op:
            batch_op.add_column(sa.Column("acceptance_mode", sa.String(length=20)))
            batch_op.add_column(sa.Column("accepted_by", sa.String(length=255)))
            batch_op.add_column(sa.Column("accepted_by_user_id", sa.String(length=36)))
            batch_op.add_column(
                sa.Column("accepted_at", sa.DateTime(timezone=True))
            )

        with op.batch_alter_table("notes") as batch_op:
            batch_op.add_column(sa.Column("archived_reason", sa.String(length=32)))
            batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True)))
            batch_op.add_column(sa.Column("archived_by", sa.String(length=255)))
            batch_op.add_column(sa.Column("archived_by_user_id", sa.String(length=36)))
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def downgrade() -> None:
    _set_sqlite_foreign_keys(enabled=False)
    try:
        with op.batch_alter_table("notes") as batch_op:
            batch_op.drop_column("archived_by_user_id")
            batch_op.drop_column("archived_by")
            batch_op.drop_column("archived_at")
            batch_op.drop_column("archived_reason")

        with op.batch_alter_table("graph_change_operations") as batch_op:
            batch_op.drop_column("accepted_at")
            batch_op.drop_column("accepted_by_user_id")
            batch_op.drop_column("accepted_by")
            batch_op.drop_column("acceptance_mode")
    finally:
        _set_sqlite_foreign_keys(enabled=True)


def _set_sqlite_foreign_keys(*, enabled: bool) -> None:
    if op.get_context().dialect.name == "sqlite":
        value = "ON" if enabled else "OFF"
        op.execute(f"PRAGMA foreign_keys={value}")
