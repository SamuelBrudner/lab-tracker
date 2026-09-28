"""Add the delegated-curation grant to graph_draft_batch_settings.

``graph_draft_batch_settings`` gains ``delegated_curation`` (NOT NULL, server
default ``off``), ``delegated_curation_granted_at`` and
``delegated_curation_granted_by``. The server default fills existing rows, so
every project keeps its proposals human-gated until an owner grants otherwise;
nothing is backfilled.

Revision ID: 0066_delegated_curation
Revises: 0065_note_hash_and_external_context_policy
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_delegated_curation"
down_revision = "0065_note_hash_and_external_context_policy"
branch_labels = None
depends_on = None

_SETTINGS_TABLE = "graph_draft_batch_settings"
_POLICY_COLUMN = "delegated_curation"
_POLICY_LENGTH = 20
_POLICY_DEFAULT = "off"
_GRANTED_AT_COLUMN = "delegated_curation_granted_at"
_GRANTED_BY_COLUMN = "delegated_curation_granted_by"
_GRANTED_BY_LENGTH = 255


def upgrade() -> None:
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
            sa.Column(_GRANTED_AT_COLUMN, sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                _GRANTED_BY_COLUMN,
                sa.String(length=_GRANTED_BY_LENGTH),
                nullable=True,
            )
        )


def downgrade() -> None:
    # SQLite drops columns by rebuilding the table; env.py suspends foreign-key
    # actions around the run so the rebuild cannot cascade into dependents.
    with op.batch_alter_table(_SETTINGS_TABLE) as batch_op:
        batch_op.drop_column(_GRANTED_BY_COLUMN)
        batch_op.drop_column(_GRANTED_AT_COLUMN)
        batch_op.drop_column(_POLICY_COLUMN)
