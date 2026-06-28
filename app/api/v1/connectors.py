"""Connectors endpoints (contract-first stubs; OAuth + live pull land at M6)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import ConnectedAccountRead, ConnectorCreate, ConnectorStart, RunAccepted

router = APIRouter(prefix="/v1/connectors", tags=["connectors"])

_NOT_YET = "connectors land at M6"


@router.post("")
async def start_connector(
    body: ConnectorCreate, user_id: Annotated[UUID, Depends(get_current_user)]
) -> ConnectorStart:
    """Begin read-only OAuth → returns the authorize URL."""
    raise NotImplementedYet(_NOT_YET)


@router.get("/callback")
async def connector_callback(
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> ConnectedAccountRead:
    """Finish OAuth → store the encrypted token (read-only scopes)."""
    raise NotImplementedYet(_NOT_YET)


@router.get("")
async def list_connectors(
    user_id: Annotated[UUID, Depends(get_current_user)],
) -> list[ConnectedAccountRead]:
    raise NotImplementedYet(_NOT_YET)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connector(
    connector_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> None:
    raise NotImplementedYet(_NOT_YET)


@router.post("/{connector_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_connector(
    connector_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> RunAccepted:
    raise NotImplementedYet(_NOT_YET)
