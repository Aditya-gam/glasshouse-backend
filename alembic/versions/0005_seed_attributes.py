"""0005 — seed the 8-attribute taxonomy.

Inserts the `attributes` rows (code, label, value_type, match_method, is_art9, allowed_values)
from `app.domain.attributes` — the single source the normalizer also reads, so the API check and
the DB can't drift (the RBAC-seed pattern). Idempotent (ON CONFLICT) so it's safe on any DB.

Revision ID: 0005
Revises: 0004
"""

import json
from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.domain.attributes import ATTRIBUTES

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSERT = text(
    "INSERT INTO attributes (code, label, value_type, match_method, is_art9, allowed_values) "
    "VALUES (:code, :label, :vt, :mm, :art9, CAST(:allowed AS jsonb)) "
    "ON CONFLICT (code) DO NOTHING"
)


def upgrade() -> None:
    for spec in ATTRIBUTES:
        op.execute(
            _INSERT.bindparams(
                code=spec.code,
                label=spec.label,
                vt=spec.value_type,
                mm=spec.match_method,
                art9=spec.is_art9,
                allowed=json.dumps(list(spec.allowed_values)) if spec.allowed_values else None,
            )
        )


def downgrade() -> None:
    delete = text("DELETE FROM attributes WHERE code = :code")
    for spec in ATTRIBUTES:
        op.execute(delete.bindparams(code=spec.code))
