"""0009 — remediation frontier columns (option_key + misled_value).

The remediation worker (defend/text-remediation.md, M3.7b) persists a *frontier* of proven options
per targeted inference — minimal / stronger generalization, remove, and opt-in decoy. `option_key`
is the frontier position (two `rewrite` rows differ only by it); `misled_value` is the decoy's
plausible-but-false value (the read surfaces it as `DefendOptionRead.misled`). Guarded: `0001`
builds fresh databases from the live model metadata (which now includes these columns), so here they
only land on databases migrated before the model change.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE remediations
            ADD COLUMN IF NOT EXISTS option_key text NOT NULL DEFAULT 'minimal',
            ADD COLUMN IF NOT EXISTS misled_value text
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM pg_constraint
                WHERE conname = 'ck_remediation_option_key'
                  AND conrelid = 'remediations'::regclass
            ) THEN
                ALTER TABLE remediations ADD CONSTRAINT ck_remediation_option_key
                    CHECK (option_key IN ('minimal','stronger','remove','decoy'));
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE remediations DROP CONSTRAINT IF EXISTS ck_remediation_option_key")
    op.execute(
        """
        ALTER TABLE remediations
            DROP COLUMN IF EXISTS option_key,
            DROP COLUMN IF EXISTS misled_value
        """
    )
