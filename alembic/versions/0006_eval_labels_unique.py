"""0006 — one eval label per (profile, attribute, modality).

The benchmark seed (loader-synthpai.md) upserts labels, so re-seeding updates in place instead
of duplicating; the constraint is the upsert's conflict target. Guarded: `0001` builds fresh
databases from the live model metadata (which now includes this constraint), so here it only
lands on databases migrated before the model change.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM pg_constraint
                WHERE conname = 'uq_eval_labels_profile_attr_modality'
                  AND conrelid = 'eval_labels'::regclass
            ) THEN
                ALTER TABLE eval_labels ADD CONSTRAINT uq_eval_labels_profile_attr_modality
                    UNIQUE (profile_id, attribute_code, modality);
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE eval_labels DROP CONSTRAINT IF EXISTS uq_eval_labels_profile_attr_modality"
    )
