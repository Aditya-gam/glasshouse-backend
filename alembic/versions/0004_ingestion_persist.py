"""0004 — ingestion-persist schema for M1.3 (encrypt + content_hmac dedupe + embed).

Reconciles ``items`` for the persist path:
  - ``embedding`` vector(1536) → **vector(384)** (fastembed ``bge-small-en-v1.5``),
  - + ``posted_at timestamptz`` and ``original_tz text`` (the temporal/timezone signal carried by
    the canonical item; the M1.1 carry-forward),
  - + ``UNIQUE (owner_user_id, content_hmac)`` for race-safe per-tenant dedupe.

Written **idempotently**: ``0001`` builds the schema from the live models, so a fresh database
already has all of this (these statements are no-ops) and only an already-migrated database gets
the deltas. The embedding column is empty at this point, so the dimension change is a safe retype.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMBEDDING_DIM = 384

_HNSW = "CREATE INDEX IF NOT EXISTS idx_items_embedding_hnsw ON items USING hnsw (embedding vector_cosine_ops)"  # noqa: E501

_UPGRADE: tuple[str, ...] = (
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS posted_at timestamptz",
    "ALTER TABLE items ADD COLUMN IF NOT EXISTS original_tz text",
    # Retype the (empty) embedding column; the HNSW index must be dropped first as it depends on it.
    "DROP INDEX IF EXISTS idx_items_embedding_hnsw",
    f"ALTER TABLE items ALTER COLUMN embedding TYPE vector({_EMBEDDING_DIM})",
    _HNSW,
    # Per-tenant dedupe constraint, guarded (a fresh DB already has it from 0001's create_all).
    """
    DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'uq_items_owner_content_hmac'
        ) THEN
            ALTER TABLE items
                ADD CONSTRAINT uq_items_owner_content_hmac UNIQUE (owner_user_id, content_hmac);
        END IF;
    END $$
    """,
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE items DROP CONSTRAINT IF EXISTS uq_items_owner_content_hmac")
    op.execute("DROP INDEX IF EXISTS idx_items_embedding_hnsw")
    op.execute("ALTER TABLE items ALTER COLUMN embedding TYPE vector(1536)")
    op.execute(_HNSW)
    op.execute("ALTER TABLE items DROP COLUMN IF EXISTS original_tz")
    op.execute("ALTER TABLE items DROP COLUMN IF EXISTS posted_at")
