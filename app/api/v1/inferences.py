"""Inferences endpoints (contract-first stubs; real data lands at M1.7+).

The tracer persists a minimal inference, but the calibrated AttributeRead/AttributeFindingRead
shapes need the normalizer + calibration (M1.7/M2), so these return 501 until then.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import (
    AttributeFindingRead,
    AttributeRead,
    InferenceConfirm,
    RemediationCreate,
    RunAccepted,
)

router = APIRouter(prefix="/v1/inferences", tags=["inferences"])

_NOT_YET = "calibrated inference reads land at M1.7+"


@router.get("")
async def list_inferences(
    user_id: Annotated[UUID, Depends(get_current_user)],
    run_id: UUID | None = None,
    profile_id: UUID | None = None,
) -> list[AttributeRead]:
    raise NotImplementedYet(_NOT_YET)


@router.get("/{inference_id}")
async def get_inference(
    inference_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> AttributeFindingRead:
    """Ranked candidates with calibrated reliability + the evidence join."""
    raise NotImplementedYet(_NOT_YET)


@router.post("/{inference_id}/confirm")
async def confirm_inference(
    inference_id: UUID,
    body: InferenceConfirm,
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> AttributeFindingRead:
    raise NotImplementedYet(_NOT_YET)


@router.post("/{inference_id}/remediations", status_code=status.HTTP_202_ACCEPTED)
async def create_remediation(
    inference_id: UUID,
    body: RemediationCreate,
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> RunAccepted:
    """Trigger a remediation run; decoy requires per-use consent. Lands at M3."""
    raise NotImplementedYet("remediation lands at M3")
