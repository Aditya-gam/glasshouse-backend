"""Data access for `eval_labels` — benchmark ground truth (synthetic; no data subject).

Written only by the privileged seed (the app role has no grant yet — the M2.2 eval service adds
read access). One label per (profile, attribute, modality); re-seeding updates in place.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def upsert_eval_label(
    conn: AsyncConnection,
    *,
    profile_id: UUID,
    attribute_code: str,
    true_value: dict[str, Any],
    modality: str,
) -> None:
    """Insert or refresh one ground-truth label for a benchmark profile."""
    await conn.execute(
        text(
            "INSERT INTO eval_labels (profile_id, attribute_code, true_value, modality) "
            "VALUES (:profile_id, :code, CAST(:tv AS jsonb), :modality) "
            "ON CONFLICT (profile_id, attribute_code, modality) "
            "DO UPDATE SET true_value = EXCLUDED.true_value"
        ),
        {
            "profile_id": profile_id,
            "code": attribute_code,
            "tv": json.dumps(true_value),
            "modality": modality,
        },
    )
