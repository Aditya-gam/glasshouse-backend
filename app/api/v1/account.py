"""Account endpoints — data-subject rights + consent (contract-first stubs; services at M0.7+)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import AccountRead, ConsentRead, ConsentUpdate, RetentionUpdate, RunAccepted

router = APIRouter(prefix="/v1/account", tags=["account"])

_NOT_YET = "account services land at M0.7 / M0.9 / M1.10"


@router.get("")
async def get_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> AccountRead:
    """Profile + consents + linked accounts + retention setting."""
    raise NotImplementedYet(_NOT_YET)


@router.post("/consents")
async def grant_consent(
    body: ConsentUpdate, user_id: Annotated[UUID, Depends(get_current_user)]
) -> ConsentRead:
    raise NotImplementedYet(_NOT_YET)


@router.delete("/consents")
async def revoke_consent(
    body: ConsentUpdate, user_id: Annotated[UUID, Depends(get_current_user)]
) -> ConsentRead:
    raise NotImplementedYet(_NOT_YET)


@router.put("/retention")
async def set_retention(
    body: RetentionUpdate, user_id: Annotated[UUID, Depends(get_current_user)]
) -> AccountRead:
    raise NotImplementedYet(_NOT_YET)


@router.post("/export", status_code=status.HTTP_202_ACCEPTED)
async def export_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> RunAccepted:
    """DSAR export bundle (expiring authenticated download)."""
    raise NotImplementedYet(_NOT_YET)


@router.delete("", status_code=status.HTTP_202_ACCEPTED)
async def erase_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> RunAccepted:
    """Erasure — crypto-shred + cascade + object deletion."""
    raise NotImplementedYet(_NOT_YET)
