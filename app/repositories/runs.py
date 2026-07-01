"""Data access for `runs` — the only place run SQL lives. Every statement is RLS-scoped."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class RunRow:
    id: UUID
    type: str
    status: str
    engine_version: str | None
    created_at: datetime
    finished_at: datetime | None


async def insert_run_v2(
    conn: AsyncConnection,
    profile_id: UUID,
    *,
    run_type: str,
    status: str,
    engine_version: str,
    idempotency_key: str | None = None,
) -> UUID:
    """Insert a run against the v2 schema (profile-scoped); the M1.7+ attack path. RLS-scoped."""
    result = await conn.execute(
        text(
            "INSERT INTO runs (profile_id, type, status, engine_version, idempotency_key) "
            "VALUES (:profile_id, :type, :status, :ev, :idem) RETURNING id"
        ),
        {
            "profile_id": profile_id,
            "type": run_type,
            "status": status,
            "ev": engine_version,
            "idem": idempotency_key,
        },
    )
    run_id: UUID = result.scalar_one()
    return run_id


async def get_run_by_idempotency_key(conn: AsyncConnection, idempotency_key: str) -> RunRow | None:
    """Return a prior run created with this key (RLS-scoped to the caller), or None.

    Lets `POST /v1/runs` dedupe client retries — the same key returns the same run, no re-run.
    """
    result = await conn.execute(
        text(
            "SELECT id, type, status, engine_version, created_at, finished_at "
            "FROM runs WHERE idempotency_key = :idem"
        ),
        {"idem": idempotency_key},
    )
    row = result.first()
    if row is None:
        return None
    return RunRow(
        id=row[0],
        type=row[1],
        status=row[2],
        engine_version=row[3],
        created_at=row[4],
        finished_at=row[5],
    )


async def set_run_status_where(
    conn: AsyncConnection,
    run_id: UUID,
    status: str,
    *,
    allowed_from: Sequence[str],
    finished: bool = False,
) -> bool:
    """Transition a run to `status` only if it is currently in `allowed_from`; True if applied.

    The guard serializes the worker and the cancel endpoint on the row (a plain `WHERE id` races):
    the worker claims `queued → running`, terminal transitions apply only from `running`, and cancel
    applies only from `queued`/`running` — so a canceled run is never resurrected and a finished run
    is never overwritten. Stamps `finished_at` at a terminal state.
    """
    result = await conn.execute(
        text(
            "UPDATE runs SET status = :status, "
            "finished_at = CASE WHEN :finished THEN now() ELSE finished_at END "
            "WHERE id = :id AND status = ANY(:allowed) RETURNING id"
        ),
        {"status": status, "finished": finished, "id": run_id, "allowed": list(allowed_from)},
    )
    return result.first() is not None


async def get_run(conn: AsyncConnection, run_id: UUID) -> RunRow | None:
    """Return the run, or None if absent or RLS-hidden (another user's run)."""
    result = await conn.execute(
        text(
            "SELECT id, type, status, engine_version, created_at, finished_at "
            "FROM runs WHERE id = :id"
        ),
        {"id": run_id},
    )
    row = result.first()
    if row is None:
        return None
    return RunRow(
        id=row[0],
        type=row[1],
        status=row[2],
        engine_version=row[3],
        created_at=row[4],
        finished_at=row[5],
    )
