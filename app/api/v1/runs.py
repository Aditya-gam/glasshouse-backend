"""Runs endpoints — thin: validate + delegate to the service. problem+json polish is M5.1.

POST creates and (for the tracer) runs the attack synchronously, returning the published
202 + {run_id} shape; M1.9 moves execution onto the arq queue with no contract change.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_current_user, get_gateway_client, get_master_key, get_scoped_session
from app.api.v1.schemas import InferenceRead, RunAccepted, RunCreate, RunRead
from app.gateway.client import GatewayClient
from app.repositories import inferences as inferences_repo
from app.repositories import runs as runs_repo
from app.services.inference import run_attack

router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: RunCreate,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    gateway: Annotated[GatewayClient, Depends(get_gateway_client)],
    user_id: Annotated[UUID, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
) -> RunAccepted:
    """Create an attack run, execute it synchronously (M1.9 → queue), and return 202 + run_id."""
    run_id = await run_attack(
        conn, gateway, owner_user_id=user_id, attribute=body.attribute, master_key=master_key
    )
    return RunAccepted(run_id=run_id, status="succeeded")


@router.get("/{run_id}")
async def read_run(
    run_id: UUID,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    master_key: Annotated[str, Depends(get_master_key)],
) -> RunRead:
    """Return the run and its inferences; 404 if absent or owned by another user (RLS-hidden)."""
    run = await runs_repo.get_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    inferences = await inferences_repo.get_run_inferences(conn, run_id, master_key)
    return RunRead(
        run_id=run.id,
        type=run.type,
        status=run.status,
        engine_version=run.engine_version,
        created_at=run.created_at,
        finished_at=run.finished_at,
        inferences=[
            InferenceRead(
                attribute=row.attribute,
                status=row.status,
                top_value=row.top_value,
                reasoning=row.reasoning,
            )
            for row in inferences
        ],
    )
