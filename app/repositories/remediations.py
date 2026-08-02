"""Data access for `remediations` — the only place remediation SQL lives (defend v2, M3.7).

One advise-only, proven before→after per targeted inference. The suggested rewrite is a **T2**
column: encrypted at rest with the owner's DEK inside `encrypt_field` (the DEK never leaves
Postgres), so plaintext is never stored in a column or interpolated into SQL. Every statement is
RLS-scoped by `profile_id`. Metrics/intervals are stored as JSONB. Content is never logged.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_remediation(
    conn: AsyncConnection,
    *,
    profile_id: UUID,
    inference_id: UUID,
    run_id: UUID,
    owner_user_id: UUID,
    master_key: str,
    action: str,
    option_key: str,
    edited_text: str | None,
    span_changes: list[dict[str, Any]],
    misled_value: str | None,
    confidence_before: float,
    confidence_after: float,
    ci_before: Mapping[str, float],
    ci_after: Mapping[str, float],
    significant: bool,
    value_recovery_before: bool,
    value_recovery_after: bool,
    utility_score: Mapping[str, Any],
    is_decoy: bool,
    evaluator_engine_version: str,
) -> UUID:
    """Insert one proven remediation; `edited_text` is encrypted at rest (NULL stays NULL).

    The before/after are the held-out evaluator adversary's calibrated confidences + bootstrap
    intervals; `significant` is the noise-floor verdict and `value_recovery_*` the top-3 flip — the
    honest, advise-only claim the frontier renders. JSONB columns are bound + cast, never built.
    """
    result = await conn.execute(
        text(
            "INSERT INTO remediations ("
            "  profile_id, inference_id, run_id, action, option_key, edited_text_ct, "
            "  span_changes, misled_value, confidence_before, confidence_after, ci_before, "
            "  ci_after, significant, value_recovery_before, value_recovery_after, utility_score, "
            "  is_decoy, evaluator_engine_version"
            ") VALUES ("
            "  :profile_id, :inference_id, :run_id, :action, :option_key, "
            "  encrypt_field(:owner, :edited_text, :mk), CAST(:span_changes AS jsonb), "
            "  :misled_value, :conf_before, :conf_after, CAST(:ci_before AS jsonb), "
            "  CAST(:ci_after AS jsonb), :significant, :vr_before, :vr_after, "
            "  CAST(:utility AS jsonb), :is_decoy, :ev"
            ") RETURNING id"
        ),
        {
            "profile_id": profile_id,
            "inference_id": inference_id,
            "run_id": run_id,
            "action": action,
            "option_key": option_key,
            "owner": owner_user_id,
            "edited_text": edited_text,
            "mk": master_key,
            "span_changes": json.dumps(span_changes),
            "misled_value": misled_value,
            "conf_before": Decimal(str(confidence_before)),
            "conf_after": Decimal(str(confidence_after)),
            "ci_before": json.dumps(dict(ci_before)),
            "ci_after": json.dumps(dict(ci_after)),
            "significant": significant,
            "vr_before": value_recovery_before,
            "vr_after": value_recovery_after,
            "utility": json.dumps(dict(utility_score)),
            "is_decoy": is_decoy,
            "ev": evaluator_engine_version,
        },
    )
    remediation_id: UUID = result.scalar_one()
    return remediation_id


# --- reads (defend screen): the persisted frontier for a run + its target inference ------------


@dataclass(frozen=True)
class FrontierRow:
    """One persisted frontier option (decrypted) — the read layer assembles these into options."""

    option_key: str
    action: str
    edited_text: str | None
    span_changes: list[dict[str, Any]]
    misled_value: str | None
    confidence_before: Decimal
    confidence_after: Decimal
    ci_before: dict[str, Any]
    ci_after: dict[str, Any]
    significant: bool
    value_recovery_before: bool
    value_recovery_after: bool
    utility_score: dict[str, Any]
    is_decoy: bool


def _to_frontier_row(row: Any) -> FrontierRow:
    return FrontierRow(
        option_key=row[0],
        action=row[1],
        edited_text=row[2],
        span_changes=row[3] or [],
        misled_value=row[4],
        confidence_before=row[5],
        confidence_after=row[6],
        ci_before=row[7] or {},
        ci_after=row[8] or {},
        significant=row[9],
        value_recovery_before=row[10],
        value_recovery_after=row[11],
        utility_score=row[12] or {},
        is_decoy=row[13],
    )


async def list_frontier_by_run(
    conn: AsyncConnection, run_id: UUID, master_key: str
) -> list[FrontierRow]:
    """Every persisted option for one remediation run (RLS-scoped); the edit is decrypted in-query.

    Ordered by the frontier position (minimal → stronger → remove → decoy) so the read renders the
    privacy/utility curve left to right. Empty when the run produced no options (un-localizable).
    """
    result = await conn.execute(
        text(
            "SELECT option_key, action, "
            "  CASE WHEN edited_text_ct IS NOT NULL "
            "       THEN decrypt_field(app_user_id(), edited_text_ct, :mk)::text ELSE NULL END, "
            "  span_changes, misled_value, confidence_before, confidence_after, ci_before, "
            "  ci_after, significant, value_recovery_before, value_recovery_after, utility_score, "
            "  is_decoy "
            "FROM remediations WHERE run_id = :run "
            "ORDER BY array_position("
            "  ARRAY['minimal','stronger','remove','decoy'], option_key), created_at"
        ),
        {"mk": master_key, "run": run_id},
    )
    return [_to_frontier_row(row) for row in result]


async def latest_remediation_run_ids(conn: AsyncConnection, inference_id: UUID) -> list[UUID]:
    """The remediation runs that produced options for `inference_id`, newest first (RLS-scoped).

    Each run is one proven frontier (one `RemediationRead`); the read assembles one per run.
    """
    result = await conn.execute(
        text(
            "SELECT run_id, max(created_at) AS latest FROM remediations "
            "WHERE inference_id = :inf GROUP BY run_id ORDER BY latest DESC"
        ),
        {"inf": inference_id},
    )
    return [row[0] for row in result]


async def get_run_inference_id(conn: AsyncConnection, run_id: UUID) -> UUID | None:
    """The inference a remediation run targeted (RLS-scoped), or None if the run has no options."""
    result = await conn.execute(
        text("SELECT inference_id FROM remediations WHERE run_id = :run LIMIT 1"),
        {"run": run_id},
    )
    row = result.first()
    return None if row is None else row[0]
