"""Dedupe unslotted goal links in a forward-only revision."""

from __future__ import annotations

from alembic import op

revision = "0041_dedupe_unslotted_goal_links"
down_revision = "0040_drop_daily_reviews_align_batch_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM goal_links
        WHERE link_id IN (
            SELECT link_id
            FROM (
                SELECT
                    link_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY goal_id, entity_type, entity_id, relation
                        ORDER BY
                            CASE WHEN created_at IS NULL THEN 1 ELSE 0 END,
                            created_at,
                            link_id
                    ) AS row_number
                FROM goal_links
                WHERE slot IS NULL OR slot = ''
            ) ranked_unslotted_links
            WHERE row_number > 1
        )
        """
    )


def downgrade() -> None:
    pass
