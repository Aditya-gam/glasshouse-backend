"""Data access for `inferences` — the only place inference SQL lives.

Tracer-bullet slice: the encrypted `reasoning_ct` round-trip and RLS scoping. The
normalized candidates/evidence children and the run/profile FKs arrive with the
attack pipeline (M1.7–M1.9).
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_inference(
    conn: AsyncConnection,
    owner_user_id: UUID,
    attribute_code: str,
    reasoning: str,
    master_key: str,
) -> UUID:
    """Insert one inference with encrypted reasoning; return its id."""
    result = await conn.execute(
        text(
            "INSERT INTO inferences (owner_user_id, attribute_code, reasoning_ct) "
            "VALUES (:owner, :attr, encrypt_field(:owner, :reasoning, :mk)) "
            "RETURNING id"
        ),
        {"owner": owner_user_id, "attr": attribute_code, "reasoning": reasoning, "mk": master_key},
    )
    inference_id: UUID = result.scalar_one()
    return inference_id


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
