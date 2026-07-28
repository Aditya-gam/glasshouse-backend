"""Data access for `remediations` — the only place remediation SQL lives (defend v2, M3.7).

One advise-only, proven before→after per targeted inference. The suggested rewrite is a **T2**
column: encrypted at rest with the owner's DEK inside `encrypt_field` (the DEK never leaves
Postgres), so plaintext is never stored in a column or interpolated into SQL. Every statement is
RLS-scoped by `profile_id`. Metrics/intervals are stored as JSONB. Content is never logged.
"""

import json
from collections.abc import Mapping
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
    edited_text: str | None,
    span_changes: list[dict[str, Any]],
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
            "  profile_id, inference_id, run_id, action, edited_text_ct, span_changes, "
            "  confidence_before, confidence_after, ci_before, ci_after, significant, "
            "  value_recovery_before, value_recovery_after, utility_score, is_decoy, "
            "  evaluator_engine_version"
            ") VALUES ("
            "  :profile_id, :inference_id, :run_id, :action, "
            "  encrypt_field(:owner, :edited_text, :mk), CAST(:span_changes AS jsonb), "
            "  :conf_before, :conf_after, CAST(:ci_before AS jsonb), CAST(:ci_after AS jsonb), "
            "  :significant, :vr_before, :vr_after, CAST(:utility AS jsonb), :is_decoy, :ev"
            ") RETURNING id"
        ),
        {
            "profile_id": profile_id,
            "inference_id": inference_id,
            "run_id": run_id,
            "action": action,
            "owner": owner_user_id,
            "edited_text": edited_text,
            "mk": master_key,
            "span_changes": json.dumps(span_changes),
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
