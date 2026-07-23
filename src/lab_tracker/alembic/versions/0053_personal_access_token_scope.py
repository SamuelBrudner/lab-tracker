"""Add a scope column to personal access tokens.

A scope narrows what an lpat_ service token may do below its role. The default
"all" preserves the existing role-based policy for every current token; the
"batch_run_due" scope authorizes only POST /batches/run-due so a scheduler token
(the daily-review automation) cannot read or write anything else if leaked.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0053_personal_access_token_scope"
down_revision = "0052_project_question_client_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("personal_access_tokens") as batch_op:
        batch_op.add_column(
            sa.Column("scope", sa.String(length=40), nullable=False, server_default="all")
        )


def downgrade() -> None:
    with op.batch_alter_table("personal_access_tokens") as batch_op:
        batch_op.drop_column("scope")
