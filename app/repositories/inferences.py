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


# --- v2 attack persistence (M1.7): inference + candidates + evidence ------------------------


async def insert_inference_v2(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    profile_id: UUID,
    owner_user_id: UUID,
    attribute_code: str,
    status: str,
    engine_version: str,
    reasoning: str | None,
    reasoning_reveals_art9: bool,
    master_key: str,
) -> UUID:
    """Insert one canonical inference; reasoning is encrypted at rest (NULL stays NULL)."""
    result = await conn.execute(
        text(
            "INSERT INTO inferences (run_id, profile_id, attribute_code, modality, status, "
            "engine_version, reasoning_ct, reasoning_reveals_art9) "
            "VALUES (:run_id, :profile_id, :attr, 'text', :status, :ev, "
            "        encrypt_field(:owner, :reasoning, :mk), :art9) RETURNING id"
        ),
        {
            "run_id": run_id,
            "profile_id": profile_id,
            "attr": attribute_code,
            "status": status,
            "ev": engine_version,
            "owner": owner_user_id,
            "reasoning": reasoning,
            "mk": master_key,
            "art9": reasoning_reveals_art9,
        },
    )
    inference_id: UUID = result.scalar_one()
    return inference_id


async def insert_candidate(
    conn: AsyncConnection,
    *,
    inference_id: UUID,
    rank: int,
    value_json: str,
    raw_confidence: float,
    confidence_source: str,
    owner_user_id: UUID,
    is_art9: bool,
    master_key: str,
) -> UUID:
    """Insert a ranked candidate; the value JSON is encrypted (`value_ct`) for Art. 9 attributes."""
    params: dict[str, object] = {
        "inf": inference_id,
        "rank": rank,
        "value": value_json,
        "raw": raw_confidence,
        "source": confidence_source,
    }
    if is_art9:
        params |= {"owner": owner_user_id, "mk": master_key}
        sql = (
            "INSERT INTO inference_candidates (inference_id, rank, value_ct, raw_confidence, "
            "confidence_source) VALUES (:inf, :rank, encrypt_field(:owner, :value, :mk), "
            ":raw, :source) RETURNING id"
        )
    else:
        sql = (
            "INSERT INTO inference_candidates (inference_id, rank, value, raw_confidence, "
            "confidence_source) VALUES (:inf, :rank, CAST(:value AS jsonb), :raw, :source) "
            "RETURNING id"
        )
    candidate_id: UUID = (await conn.execute(text(sql), params)).scalar_one()
    return candidate_id


async def insert_evidence(
    conn: AsyncConnection,
    *,
    candidate_id: UUID,
    ref_type: str,
    ref_id: UUID,
    span_json: str | None,
    rationale: str | None,
    owner_user_id: UUID,
    master_key: str,
) -> None:
    """Insert one evidence row; `span` is JSONB and `rationale` is encrypted at rest."""
    await conn.execute(
        text(
            "INSERT INTO inference_evidence (candidate_id, ref_type, ref_id, modality, span, "
            "rationale_ct) VALUES (:cand, :ref_type, :ref_id, 'text', CAST(:span AS jsonb), "
            "encrypt_field(:owner, :rationale, :mk))"
        ),
        {
            "cand": candidate_id,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "span": span_json,
            "rationale": rationale,
            "owner": owner_user_id,
            "mk": master_key,
        },
    )
