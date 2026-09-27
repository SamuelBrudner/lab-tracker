"""Index the note evidence content hash and add the external-context policy.

* ``notes.evidence_content_hash`` mirrors ``metadata["evidence_content_hash"]``
  into a nullable String(255) column so the content-hash detector and
  ``GET /notes?evidence_content_hash=`` are served by the composite index
  ``ix_notes_project_evidence_content_hash`` instead of JSON extraction. The
  column is backfilled from the JSON with a dialect-explicit expression
  (``json_extract`` on SQLite, ``->>`` on PostgreSQL; any other dialect stops
  the upgrade). A value longer than 255 characters stops the upgrade with a
  count and sample note ids before anything changes. The metadata JSON itself
  is never modified.
* ``graph_draft_batch_settings`` gains ``external_context_policy`` (NOT NULL,
  server default ``own_notes_only``), ``external_provider_acknowledged_at`` and
  ``external_provider_acknowledged_by``. The server default fills existing
  rows; nothing is backfilled.

Revision ID: 0065_note_hash_and_external_context_policy
Revises: 0064_orm_schema_parity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_note_hash_and_external_context_policy"
down_revision = "0064_orm_schema_parity"
branch_labels = None
depends_on = None

_DIAGNOSTIC_SAMPLE_LIMIT = 5

_NOTES_TABLE = "notes"
_HASH_COLUMN = "evidence_content_hash"
_HASH_INDEX = "ix_notes_project_evidence_content_hash"
_HASH_MAX_LENGTH = 255

_SETTINGS_TABLE = "graph_draft_batch_settings"
_POLICY_COLUMN = "external_context_policy"
_POLICY_LENGTH = 20
_POLICY_DEFAULT = "own_notes_only"
_ACKNOWLEDGED_AT_COLUMN = "external_provider_acknowledged_at"
_ACKNOWLEDGED_BY_COLUMN = "external_provider_acknowledged_by"
_ACKNOWLEDGED_BY_LENGTH = 255


def upgrade() -> None:
    expression = _hash_expression(op.get_bind().dialect.name)
    # Reads only the metadata JSON, so it runs before any schema change and a
    # refusal leaves the database exactly as it was.
    _preflight_hash_lengths(expression)
    with op.batch_alter_table(_NOTES_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(_HASH_COLUMN, sa.String(length=_HASH_MAX_LENGTH), nullable=True)
        )
    _backfill_hash_column(expression)
    op.create_index(_HASH_INDEX, _NOTES_TABLE, ["project_id", _HASH_COLUMN])
    with op.batch_alter_table(_SETTINGS_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                _POLICY_COLUMN,
                sa.String(length=_POLICY_LENGTH),
                nullable=False,
                server_default=_POLICY_DEFAULT,
            )
        )
        batch_op.add_column(
            sa.Column(_ACKNOWLEDGED_AT_COLUMN, sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                _ACKNOWLEDGED_BY_COLUMN,
                sa.String(length=_ACKNOWLEDGED_BY_LENGTH),
                nullable=True,
            )
        )


def downgrade() -> None:
    # SQLite drops columns by rebuilding the table; env.py suspends foreign-key
    # actions around the run so the rebuild cannot cascade into dependents.
    with op.batch_alter_table(_SETTINGS_TABLE) as batch_op:
        batch_op.drop_column(_ACKNOWLEDGED_BY_COLUMN)
        batch_op.drop_column(_ACKNOWLEDGED_AT_COLUMN)
        batch_op.drop_column(_POLICY_COLUMN)
    op.drop_index(_HASH_INDEX, table_name=_NOTES_TABLE)
    with op.batch_alter_table(_NOTES_TABLE) as batch_op:
        batch_op.drop_column(_HASH_COLUMN)


def _hash_expression(dialect_name: str) -> str:
    """SQL extracting the hash text from the notes.metadata JSON, per dialect."""

    if dialect_name == "sqlite":
        return f"json_extract(\"metadata\", '$.{_HASH_COLUMN}')"
    if dialect_name == "postgresql":
        return f"\"metadata\" ->> '{_HASH_COLUMN}'"
    raise RuntimeError(
        f"Cannot apply migration {revision}: no JSON extraction expression is "
        f"defined for the {dialect_name!r} dialect (only sqlite and postgresql "
        "are supported). No rows were changed."
    )


def _preflight_hash_lengths(expression: str) -> None:
    connection = op.get_bind()
    over_long = f"FROM {_NOTES_TABLE} WHERE length({expression}) > {_HASH_MAX_LENGTH}"
    samples = (
        connection.execute(
            sa.text(
                f"SELECT note_id {over_long} ORDER BY note_id LIMIT {_DIAGNOSTIC_SAMPLE_LIMIT}"
            )
        )
        .scalars()
        .all()
    )
    if not samples:
        return
    count = int(connection.scalar(sa.text(f"SELECT COUNT(*) {over_long}")) or 0)
    rendered = ", ".join(f"note_id={value}" for value in samples)
    remaining = count - len(samples)
    suffix = f", plus {remaining} more" if remaining > 0 else ""
    raise RuntimeError(
        f"Cannot apply migration {revision}: metadata.{_HASH_COLUMN} is longer than "
        f"{_HASH_MAX_LENGTH} characters in {count} note(s) ({rendered}{suffix}). "
        "No rows were changed; shorten those metadata values, then retry the migration."
    )


def _backfill_hash_column(expression: str) -> None:
    op.get_bind().execute(
        sa.text(
            f"UPDATE {_NOTES_TABLE} SET {_HASH_COLUMN} = {expression} "
            f"WHERE {expression} IS NOT NULL AND {expression} <> ''"
        )
    )
