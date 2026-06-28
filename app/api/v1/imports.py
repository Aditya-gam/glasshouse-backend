"""Imports endpoints (contract-first stubs; ingestion lands at M1.1–M1.3)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import ImportRead, RunAccepted

router = APIRouter(prefix="/v1/imports", tags=["imports"])

_NOT_YET = "ingestion lands at M1.1–M1.3"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_import(user_id: Annotated[UUID, Depends(get_current_user)]) -> RunAccepted:
    """Upload an export/photo set → an ingestion run (202 + run_id)."""
    raise NotImplementedYet(_NOT_YET)


@router.get("")
async def list_imports(user_id: Annotated[UUID, Depends(get_current_user)]) -> list[ImportRead]:
    raise NotImplementedYet(_NOT_YET)


@router.get("/{import_id}")
async def get_import(
    import_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> ImportRead:
    raise NotImplementedYet(_NOT_YET)
