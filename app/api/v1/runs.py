"""Runs endpoints. POST + GET are live (from T4); list/SSE/cancel are contract-first stubs.

POST creates and (for the tracer) runs the attack synchronously, returning the published
202 + {run_id} shape; M1.9 moves execution onto the arq queue with no contract change.
"""

from typing import Annotated, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_current_user, get_gateway_client, get_master_key, get_scoped_session
from app.api.errors import NotFound, NotImplementedYet
from app.api.v1.schemas import RunAccepted, RunCreate, RunStatus
from app.domain.output_schema import AttributeCode
from app.gateway.client import GatewayClient
from app.repositories import runs as runs_repo
from app.services.inference import run_attack

router = APIRouter(prefix="/v1/runs", tags=["runs"])

_DEFAULT_ATTRIBUTE = "location"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: RunCreate,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    gateway: Annotated[GatewayClient, Depends(get_gateway_client)],
    user_id: Annotated[UUID, Depends(get_current_user)],
    master_key: Annotated[str, Depends(get_master_key)],
) -> RunAccepted:
    """Create a run, execute it synchronously (M1.9 → queue), and return 202 + run_id.

    Only `attack` is live for the tracer; `eval`/`remediation` arrive with M2/M3.
    """
    if body.type != "attack":
        raise NotImplementedYet(f"run type '{body.type}' lands with its engine milestone")
    attribute = str(body.params.get("attribute", _DEFAULT_ATTRIBUTE))
    if attribute not in get_args(AttributeCode):
        raise NotFound(f"unknown attribute '{attribute}'")
    run_id = await run_attack(
        conn,
        gateway,
        owner_user_id=user_id,
        attribute=attribute,  # type: ignore[arg-type]  # validated against AttributeCode above
        master_key=master_key,
    )
    return RunAccepted(run_id=run_id, status="succeeded")


@router.get("/{run_id}")
async def read_run(
    run_id: UUID,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
) -> RunStatus:
    """Poll a run; 404 if absent or owned by another user (RLS-hidden)."""
    run = await runs_repo.get_run(conn, run_id)
    if run is None:
        raise NotFound("run not found")
    return RunStatus(
        id=run.id,
        type=run.type,
        status=run.status,
        engine_version=run.engine_version,
    )


@router.get("")
async def list_runs(user_id: Annotated[UUID, Depends(get_current_user)]) -> list[RunStatus]:
    """Cursor-paginated list — lands with M5.2 (routers + pagination)."""
    raise NotImplementedYet("run listing lands with M5.2")


@router.get("/{run_id}/events")
async def run_events(
    run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> RunStatus:
    """SSE live progress — lands with M5.2 (poll + SSE)."""
    raise NotImplementedYet("SSE progress lands with M5.2")


@router.post("/{run_id}:cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> RunStatus:
    """Request cancellation — lands with the arq worker (M1.9)."""
    raise NotImplementedYet("run cancellation lands with M1.9")
