"""Data access for `inferences` — the only place inference SQL lives (v2 attack schema).

The canonical inference + its ranked candidates + evidence children, all RLS-scoped; Art. 9 values
and reasoning are encrypted at rest (`encrypt_field`, DEK never leaves Postgres).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
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
    calibrated_reliability: Decimal | None = None,
) -> UUID:
    """Insert a ranked candidate; the value JSON is encrypted (`value_ct`) for Art. 9 attributes.

    `calibrated_reliability` is the per-user-scoring output (M2.5) — the calibrated point the user
    sees; NULL when the calibration map has no matching bucket (fail closed, never the raw number).
    """
    params: dict[str, object] = {
        "inf": inference_id,
        "rank": rank,
        "value": value_json,
        "raw": raw_confidence,
        "source": confidence_source,
        "calibrated": calibrated_reliability,
    }
    if is_art9:
        params |= {"owner": owner_user_id, "mk": master_key}
        sql = (
            "INSERT INTO inference_candidates (inference_id, rank, value_ct, raw_confidence, "
            "confidence_source, calibrated_reliability) "
            "VALUES (:inf, :rank, encrypt_field(:owner, :value, :mk), :raw, :source, :calibrated) "
            "RETURNING id"
        )
    else:
        sql = (
            "INSERT INTO inference_candidates (inference_id, rank, value, raw_confidence, "
            "confidence_source, calibrated_reliability) "
            "VALUES (:inf, :rank, CAST(:value AS jsonb), :raw, :source, :calibrated) RETURNING id"
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


# --- remediation target (M3.7): the attribute + profile + top-1 value being defended -----------


@dataclass(frozen=True)
class InferenceTarget:
    """What a remediation run defends: the inference's attribute, its profile, its top-1 value.

    `value` is the canonical top-1 guess (decrypted) — the value the adversary recovered and the
    user wants to break; None when the inference abstained (nothing to remediate).
    """

    attribute_code: str
    profile_id: UUID
    value: dict[str, Any] | None


async def get_inference_reliability(conn: AsyncConnection, inference_id: UUID) -> Decimal | None:
    """The inference's top-1 calibrated reliability (RLS-scoped), or None if absent/uncalibrated.

    The defend screen shows this as the target's `before` exposure when a remediation produced no
    proven options (the un-localizable / cant_break case) — there is no adversary before-sample.
    """
    result = await conn.execute(
        text(
            "SELECT c.calibrated_reliability FROM inference_candidates c "
            "WHERE c.inference_id = :inf AND c.rank = 1"
        ),
        {"inf": inference_id},
    )
    row = result.first()
    return None if row is None else row[0]


async def get_inference_profile(conn: AsyncConnection, inference_id: UUID) -> UUID | None:
    """The inference's profile id (RLS-scoped), or None if absent/hidden — no decryption needed.

    Lets the enqueue endpoint scope the remediation run to the target's profile without touching the
    master key (that stays a worker concern).
    """
    result = await conn.execute(
        text("SELECT profile_id FROM inferences WHERE id = :inf"), {"inf": inference_id}
    )
    row = result.first()
    return None if row is None else row[0]


async def get_inference_target(
    conn: AsyncConnection, inference_id: UUID, master_key: str
) -> InferenceTarget | None:
    """The target inference's attribute + profile + decrypted top-1 value, or None if absent/hidden.

    RLS-scoped: another user's inference (or a missing id) returns None so the worker fails closed.
    The Art. 9 value is decrypted in-query with the caller's DEK, so plaintext never leaves the DB.
    """
    result = await conn.execute(
        text(
            "SELECT i.attribute_code, i.profile_id, "
            "  CASE WHEN c.value IS NOT NULL THEN c.value "
            "       WHEN c.value_ct IS NOT NULL "
            "         THEN decrypt_field(app_user_id(), c.value_ct, :mk)::jsonb "
            "       ELSE NULL END AS value "
            "FROM inferences i "
            "LEFT JOIN inference_candidates c ON c.inference_id = i.id AND c.rank = 1 "
            "WHERE i.id = :inf"
        ),
        {"mk": master_key, "inf": inference_id},
    )
    row = result.first()
    if row is None:
        return None
    return InferenceTarget(attribute_code=row[0], profile_id=row[1], value=row[2])


# --- dashboard read (M2.5b): one card per attribute, top-1 candidate + calibrated reliability --


@dataclass(frozen=True)
class DashboardInference:
    """One attribute's card data: status, the top-1 value, its calibrated reliability, evidence."""

    attribute_code: str
    status: str
    value: dict[str, Any] | None  # the canonical top-1 value (decrypted), or None if abstained
    calibrated_reliability: Decimal | None
    evidence_count: int


async def list_dashboard_inferences(
    conn: AsyncConnection,
    *,
    master_key: str,
    run_id: UUID | None = None,
    profile_id: UUID | None = None,
) -> list[DashboardInference]:
    """The caller's inferences (RLS-scoped) as dashboard rows: top-1 value + calibrated reliability.

    Exactly one card per attribute — the **latest** inference for each (`DISTINCT ON` + newest
    first), so a user who has run several attacks sees their current audit, not one card per run.
    The Art. 9 value is decrypted in-query with the caller's DEK (`app_user_id()`), so plaintext
    never leaves Postgres in a column; the raw model confidence is never selected (only the
    calibrated point). Optionally filtered to one run / profile.
    """
    result = await conn.execute(
        text(
            "SELECT DISTINCT ON (i.attribute_code) i.attribute_code, i.status, "
            "  CASE WHEN c.value IS NOT NULL THEN c.value "
            "       WHEN c.value_ct IS NOT NULL "
            "         THEN decrypt_field(app_user_id(), c.value_ct, :mk)::jsonb "
            "       ELSE NULL END AS value, "
            "  c.calibrated_reliability, "
            "  COALESCE((SELECT count(*) FROM inference_evidence e "
            "            WHERE e.candidate_id = c.id), 0) "
            "FROM inferences i "
            "LEFT JOIN inference_candidates c ON c.inference_id = i.id AND c.rank = 1 "
            # cast the optional filters so asyncpg can type a NULL bound param in `IS NULL`.
            "WHERE (CAST(:run_id AS uuid) IS NULL OR i.run_id = CAST(:run_id AS uuid)) "
            "  AND (CAST(:profile_id AS uuid) IS NULL OR i.profile_id = CAST(:profile_id AS uuid)) "
            # DISTINCT ON keeps the first row per attribute → newest wins (the current audit).
            "ORDER BY i.attribute_code, i.created_at DESC"
        ),
        {"mk": master_key, "run_id": run_id, "profile_id": profile_id},
    )
    return [
        DashboardInference(
            attribute_code=row[0],
            status=row[1],
            value=row[2],
            calibrated_reliability=row[3],
            evidence_count=row[4],
        )
        for row in result
    ]
