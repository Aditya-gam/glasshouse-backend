"""Data access for `inferences` — the only place inference SQL lives (v2 attack schema).

The canonical inference + its ranked candidates + evidence children, all RLS-scoped; Art. 9 values
and reasoning are encrypted at rest (`encrypt_field`, DEK never leaves Postgres).
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
