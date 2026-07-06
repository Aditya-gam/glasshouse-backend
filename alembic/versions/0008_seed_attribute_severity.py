"""0008 — seed the taxonomy severity + sensitive tier onto the `attributes` rows.

`0005` seeded code/label/value_type/match_method/is_art9/allowed_values; this fills the two
columns the dashboard card needs — `is_sensitive_tier` (our stricter-than-legal policy tier) and
`severity` (the per-persona risk matrix). Both come from `app.domain.attributes` (the single
source), so the DB mirrors the taxonomy the API also reads. Idempotent (UPDATE by code).

Revision ID: 0008
Revises: 0007
"""

import json
from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.domain.attributes import ATTRIBUTES

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPDATE = text(
    "UPDATE attributes SET is_sensitive_tier = :sensitive, severity = CAST(:severity AS jsonb) "
    "WHERE code = :code"
)


def upgrade() -> None:
    for spec in ATTRIBUTES:
        op.execute(
            _UPDATE.bindparams(
                code=spec.code,
                sensitive=spec.is_sensitive_tier,
                severity=json.dumps(
                    {"atrisk": spec.severity.atrisk, "jobseeker": spec.severity.jobseeker}
                ),
            )
        )


def downgrade() -> None:
    op.execute(text("UPDATE attributes SET is_sensitive_tier = false, severity = NULL"))
