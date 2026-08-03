"""Inferences endpoints — the dashboard cards (M2.5b) + the attribution detail (stub until M3).

`GET /v1/inferences` serves the 8 attribute cards: the top-1 value + its **calibrated** reliability
(point + interval), the taxonomy severity/sensitivity, and an evidence count. Raw model confidence
is never serialized — only the calibrated point (per-user-scoring.md). Reliability is `null` when
the engine abstained (and, transitionally, when the calibration map has no bucket — fail closed).
"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_current_user, get_scoped_session
from app.api.errors import NotFound, NotImplementedYet
from app.api.v1.inference_assembly import assemble_finding
from app.api.v1.schemas import (
    AttributeFindingRead,
    AttributeRead,
    InferenceConfirm,
    Reliability,
    RemediationCreate,
    RunAccepted,
    Severity,
)
from app.db.crypto import get_master_key
from app.domain.attributes import BY_CODE
from app.domain.eval_match import render_value
from app.domain.output_schema import AttributeCode, AttributeValue
from app.repositories import inferences as inferences_repo
from app.services.consent import has_special_category_consent

router = APIRouter(prefix="/v1/inferences", tags=["inferences"])

_VALUE_ADAPTER: TypeAdapter[AttributeValue] = TypeAdapter(AttributeValue)


def _evidence_summary(status: str, count: int) -> str:
    """A short human affordance for the card (the detail view carries the real evidence items)."""
    if status == "abstained":
        return "no inference"
    if count == 0:
        return "no cited evidence"
    return f"{count} supporting item{'s' if count != 1 else ''}"


def _to_attribute_read(row: inferences_repo.DashboardInference) -> AttributeRead:
    """One dashboard card from a persisted inference — calibrated reliability only, not the raw."""
    spec = BY_CODE[cast(AttributeCode, row.attribute_code)]  # FK-constrained to the 8 codes
    inferred = row.status == "inferred" and row.value is not None
    value = render_value(spec.code, _VALUE_ADAPTER.validate_python(row.value)) if inferred else None
    # null reliability ⇔ no calibrated number (engine abstained, or the map has no bucket yet).
    reliability = (
        Reliability(
            point=float(row.calibrated_reliability),
            lo=float(row.calibrated_reliability),  # a point band until the noise model (M3.4)
            hi=float(row.calibrated_reliability),
        )
        if row.calibrated_reliability is not None
        else None
    )
    return AttributeRead(
        id=row.id,
        code=spec.code,
        label=spec.label,
        value=value,
        detail=None,
        reliability=reliability,
        evidence=_evidence_summary(row.status, row.evidence_count),
        evidence_count=row.evidence_count,
        abstain=row.status == "abstained",
        sensitive=spec.is_sensitive_tier,
        art9=spec.is_art9,
        severity=Severity(atrisk=spec.severity.atrisk, jobseeker=spec.severity.jobseeker),
    )


@router.get("")
async def list_inferences(
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    run_id: UUID | None = None,
    profile_id: UUID | None = None,
) -> list[AttributeRead]:
    """The user's attribute cards (RLS-scoped), optionally filtered to one run / profile."""
    rows = await inferences_repo.list_dashboard_inferences(
        conn, master_key=get_master_key(), run_id=run_id, profile_id=profile_id
    )
    return [_to_attribute_read(row) for row in rows]


@router.get("/{inference_id}")
async def get_inference(
    inference_id: UUID,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
) -> AttributeFindingRead:
    """One inference's attribution detail — ranked candidates + per-item evidence (RLS-scoped).

    Tenant-scoped to the caller's RLS context: another user's id (or a missing one) → 404, never a
    cross-tenant read (no IDOR). Special-category values/reasoning are decrypted only under valid
    Art. 9 consent and masked otherwise (fail closed). Content is decrypted in-query, never logged.
    """
    finding = await inferences_repo.get_inference_finding(
        conn,
        inference_id,
        get_master_key(),
        include_special_category=await has_special_category_consent(conn),
    )
    if finding is None:
        raise NotFound("inference not found")
    return assemble_finding(finding)


@router.post("/{inference_id}/confirm")
async def confirm_inference(
    inference_id: UUID,
    body: InferenceConfirm,
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> AttributeFindingRead:
    raise NotImplementedYet("confirm/deny lands at M3")


@router.post("/{inference_id}/remediations", status_code=status.HTTP_202_ACCEPTED)
async def create_remediation(
    inference_id: UUID,
    body: RemediationCreate,
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> RunAccepted:
    """Trigger a remediation run; decoy requires per-use consent. Lands at M3."""
    raise NotImplementedYet("remediation lands at M3")
