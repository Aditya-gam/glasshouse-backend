"""Runs endpoints. POST enqueues the async attack (202 + run_id); GET/SSE poll status; cancel
marks a non-terminal run canceled. `list` is a contract-first stub (M5.2).

POST consent-gates → creates a `queued` run → enqueues the arq worker (`app.workers.attack`),
which runs the joint self-consistency attack and drives the queued → running → terminal lifecycle.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.api.deps import (
    get_app_engine,
    get_arq_pool,
    get_current_user,
    get_scoped_session,
)
from app.api.errors import NotFound, NotImplementedYet
from app.api.v1.schemas import RunAccepted, RunCreate, RunStatus
from app.db.rls import set_rls_context
from app.gateway.prompts import ENGINE_VERSION
from app.repositories import profiles as profiles_repo
from app.repositories import runs as runs_repo
from app.services.consent import require_consent

router = APIRouter(prefix="/v1/runs", tags=["runs"])

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
_SSE_POLL_SECONDS = 1.0
_SSE_MAX_SECONDS = 30.0


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: RunCreate,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
    user_id: Annotated[UUID, Depends(get_current_user)],
    pool: Annotated[ArqRedis, Depends(get_arq_pool)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> RunAccepted:
    """Create an attack run and enqueue it; return 202 + run_id (poll `GET /runs/{id}` or the SSE).

    **Consent-gated** (fail closed). The run infers all 8 attributes jointly (M1.7+); `params` is
    reserved. A repeated `Idempotency-Key` returns the original run without re-enqueuing.
    `eval`/`remediation` arrive with M2/M3.
    """
    if body.type != "attack":
        raise NotImplementedYet(f"run type '{body.type}' lands with its engine milestone")
    await require_consent(conn, "self_audit")  # no run without a valid, non-revoked consent
    if idempotency_key is not None:
        existing = await runs_repo.get_run_by_idempotency_key(conn, idempotency_key)
        if existing is not None:
            return RunAccepted(run_id=existing.id, status=existing.status)
    profile_id = await profiles_repo.get_or_create_self_profile(conn, user_id)
    run_id = await runs_repo.insert_run_v2(
        conn,
        profile_id,
        run_type="attack",
        status="queued",
        engine_version=ENGINE_VERSION,
        idempotency_key=idempotency_key,
    )
    # _job_id = the run id → arq dedupes a double-enqueue; the worker runs after this commits.
    await pool.enqueue_job("attack_run", str(run_id), str(user_id), _job_id=f"attack:{run_id}")
    return RunAccepted(run_id=run_id, status="queued")


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


def _sse(event: str, data: str) -> str:
    """Frame one Server-Sent Event."""
    return f"event: {event}\ndata: {data}\n\n"


def _status_json(run: runs_repo.RunRow) -> str:
    return RunStatus(
        id=run.id, type=run.type, status=run.status, engine_version=run.engine_version
    ).model_dump_json()


async def _read_run_scoped(
    engine: AsyncEngine, user_id: UUID, run_id: UUID
) -> runs_repo.RunRow | None:
    """Read a run under the caller's RLS context on a fresh connection (safe inside a stream)."""
    async with engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        return await runs_repo.get_run(conn, run_id)


async def _run_event_stream(
    engine: AsyncEngine, user_id: UUID, run_id: UUID, request: Request, initial: runs_repo.RunRow
) -> AsyncIterator[str]:
    """Emit a `status` event on each change and a final `done` event at a terminal state.

    The request-scoped session can't be used here (it closes before the stream emits), so each poll
    opens its own RLS-scoped connection. M1.9's worker will drive real queued→running transitions.
    """
    yield _sse("status", _status_json(initial))
    if initial.status in _TERMINAL_STATUSES:
        yield _sse("done", _status_json(initial))
        return
    last_status = initial.status
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SSE_MAX_SECONDS
    while loop.time() < deadline:
        if await request.is_disconnected():
            return
        await asyncio.sleep(_SSE_POLL_SECONDS)
        run = await _read_run_scoped(engine, user_id, run_id)
        if run is None:  # erased mid-stream
            return
        if run.status != last_status:
            last_status = run.status
            yield _sse("status", _status_json(run))
        if run.status in _TERMINAL_STATUSES:
            yield _sse("done", _status_json(run))
            return


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {"description": "SSE stream of run status", "content": {"text/event-stream": {}}}
    },
)
async def run_events(
    run_id: UUID,
    request: Request,
    user_id: Annotated[UUID, Depends(get_current_user)],
    engine: Annotated[AsyncEngine, Depends(get_app_engine)],
) -> StreamingResponse:
    """SSE stream of run status; 404 if absent or RLS-hidden.

    Auth is the standard bearer — the FE proxies the stream and injects it server-side (decision
    (c)), since browser EventSource can't set headers.
    """
    run = await _read_run_scoped(engine, user_id, run_id)
    if run is None:
        raise NotFound("run not found")
    return StreamingResponse(
        _run_event_stream(engine, user_id, run_id, request, run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}:cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: UUID,
    conn: Annotated[AsyncConnection, Depends(get_scoped_session)],
) -> RunStatus:
    """Request cancellation. A queued/running run is marked `canceled` (the worker won't start, or
    won't finish, a canceled run); a terminal run is unchanged. 404 if absent or RLS-hidden.

    The transition is status-guarded, so it never overwrites a run that finished concurrently —
    the re-read returns the run's true current status.
    """
    await runs_repo.set_run_status_where(
        conn, run_id, "canceled", allowed_from=("queued", "running"), finished=True
    )
    run = await runs_repo.get_run(conn, run_id)
    if run is None:
        raise NotFound("run not found")
    return RunStatus(id=run.id, type=run.type, status=run.status, engine_version=run.engine_version)
