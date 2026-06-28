"""Remediations endpoints (contract-first stubs; defend lands at M3). Advise-only."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import RemediationRead

router = APIRouter(prefix="/v1/remediations", tags=["remediations"])

_NOT_YET = "defend (remediations) lands at M3"


@router.get("/{remediation_id}")
async def get_remediation(
    remediation_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> RemediationRead:
    """The proven before/after (intervals, value-recovery, significant) + frontier options."""
    raise NotImplementedYet(_NOT_YET)


@router.get("")
async def list_remediations(
    user_id: Annotated[UUID, Depends(get_current_user)],
    inference_id: UUID | None = None,
) -> list[RemediationRead]:
    raise NotImplementedYet(_NOT_YET)
