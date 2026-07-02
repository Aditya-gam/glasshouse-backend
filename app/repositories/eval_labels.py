"""Data access for `eval_labels` — benchmark ground truth (synthetic; no data subject).

Read + written only on a **privileged** connection (the seed + the eval service, both ops-time).
The app role has no grant on this table, so a user request can never reach benchmark ground truth.
One label per (profile, attribute, modality); re-seeding updates in place.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class EvalLabelRow:
    """One ground-truth label: the attribute + its `{value, hardness, certainty}` payload."""

    attribute_code: str
    true_value: dict[str, Any]


async def list_labels_for_profile(conn: AsyncConnection, profile_id: UUID) -> list[EvalLabelRow]:
    """All text-modality labels for one benchmark profile (privileged read)."""
    result = await conn.execute(
        text(
            "SELECT attribute_code, true_value FROM eval_labels "
            "WHERE profile_id = :p AND modality = 'text'"
        ),
        {"p": profile_id},
    )
    return [EvalLabelRow(attribute_code=row[0], true_value=row[1]) for row in result]


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
