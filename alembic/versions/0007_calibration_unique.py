"""0007 — one calibration row per (engine_version, attribute, modality, signal, n, bucket).

The eval service (M2.4) upserts the calibration map, so re-benchmarking the same engine refreshes
each bucket in place instead of duplicating; the constraint is the upsert's conflict target and the
map's lookup key. Guarded: `0001` builds fresh databases from the live model metadata (which now
carries this constraint), so here it only lands on databases migrated before the model change.

The unique constraint's implicit index covers the same columns as `0001`'s plain
`idx_calibration_lookup` (the M2.5 lookup index), so the plain one is now redundant — drop it to
avoid maintaining two identical btrees on every calibration upsert.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM pg_constraint
                WHERE conname = 'uq_calibration_lookup'
                  AND conrelid = 'calibration'::regclass
            ) THEN
                ALTER TABLE calibration ADD CONSTRAINT uq_calibration_lookup
                    UNIQUE (engine_version, attribute_code, modality, signal, n, confidence_bucket);
            END IF;
        END $$
        """
    )
    # the unique index above subsumes the plain lookup index from 0001 — drop the redundant one.
    op.execute("DROP INDEX IF EXISTS idx_calibration_lookup")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_calibration_lookup ON calibration "
        "(engine_version, attribute_code, modality, signal, n, confidence_bucket)"
    )
    op.execute("ALTER TABLE calibration DROP CONSTRAINT IF EXISTS uq_calibration_lookup")
