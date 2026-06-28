"""Data access for `inferences` — the only place inference SQL lives.

Tracer-bullet slice: the encrypted `reasoning_ct` round-trip and RLS scoping. The
normalized candidates/evidence children and the run/profile FKs arrive with the
attack pipeline (M1.7–M1.9).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class InferenceRow:
    attribute: str
    status: str
    top_value: str | None
    reasoning: str | None


async def insert_inference(
    conn: AsyncConnection,
    owner_user_id: UUID,
    attribute_code: str,
    reasoning: str,
    master_key: str,
    *,
    run_id: UUID | None = None,
    top_value_text: str | None = None,
    status: str = "inferred",
) -> UUID:
    """Insert one inference with encrypted reasoning; return its id."""
    result = await conn.execute(
        text(
            "INSERT INTO inferences "
            "(owner_user_id, run_id, attribute_code, status, top_value_text, reasoning_ct) "
            "VALUES (:owner, :run_id, :attr, :status, :top_value, "
            "        encrypt_field(:owner, :reasoning, :mk)) "
            "RETURNING id"
        ),
        {
            "owner": owner_user_id,
            "run_id": run_id,
            "attr": attribute_code,
            "status": status,
            "top_value": top_value_text,
            "reasoning": reasoning,
            "mk": master_key,
        },
    )
    inference_id: UUID = result.scalar_one()
    return inference_id


async def get_run_inferences(
    conn: AsyncConnection, run_id: UUID, master_key: str
) -> list[InferenceRow]:
    """All inferences for a run (RLS-scoped), with reasoning decrypted in memory."""
    result = await conn.execute(
        text(
            "SELECT attribute_code, status, top_value_text, "
            "       decrypt_field(owner_user_id, reasoning_ct, :mk) "
            "FROM inferences WHERE run_id = :run_id ORDER BY created_at"
        ),
        {"run_id": run_id, "mk": master_key},
    )
    return [
        InferenceRow(attribute=row[0], status=row[1], top_value=row[2], reasoning=row[3])
        for row in result
    ]


async def get_inference_reasoning(
    conn: AsyncConnection, inference_id: UUID, master_key: str
) -> str | None:
    """Return the decrypted reasoning, or None if absent or RLS-hidden."""
    result = await conn.execute(
        text(
            "SELECT decrypt_field(owner_user_id, reasoning_ct, :mk) FROM inferences WHERE id = :id"
        ),
        {"id": inference_id, "mk": master_key},
    )
    row = result.first()
    if row is None:
        return None
    reasoning: str = row[0]
    return reasoning
