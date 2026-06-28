"""Data access for `runs` — the only place run SQL lives. Every statement is RLS-scoped."""

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


async def create_run(
    conn: AsyncConnection,
    owner_user_id: UUID,
    *,
    run_type: str,
    status: str,
    engine_version: str | None,
) -> UUID:
    """Insert a run for the current user; return its id."""
    result = await conn.execute(
        text(
            "INSERT INTO runs (owner_user_id, type, status, engine_version) "
            "VALUES (:owner, :type, :status, :ev) RETURNING id"
        ),
        {"owner": owner_user_id, "type": run_type, "status": status, "ev": engine_version},
    )
    run_id: UUID = result.scalar_one()
    return run_id


async def set_run_status(
    conn: AsyncConnection, run_id: UUID, status: str, *, finished: bool = False
) -> None:
    """Update a run's status; stamp `finished_at` when it reaches a terminal state."""
    await conn.execute(
        text(
            "UPDATE runs SET status = :status, "
            "finished_at = CASE WHEN :finished THEN now() ELSE finished_at END "
            "WHERE id = :id"
        ),
        {"status": status, "finished": finished, "id": run_id},
    )


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
