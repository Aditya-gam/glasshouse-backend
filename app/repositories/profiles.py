"""Data access for `profiles` — the subject's audit profile. The only place profile SQL lives.

Every statement runs under the caller's RLS context (`profiles.user_id = app_user_id()`), so a
profile is only ever read/created for the scoped user.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def get_or_create_self_profile(conn: AsyncConnection, owner_user_id: UUID) -> UUID:
    """Return the user's `self` profile id, creating it on first ingestion."""
    result = await conn.execute(
        text("SELECT id FROM profiles WHERE user_id = :uid AND type = 'self'"),
        {"uid": owner_user_id},
    )
    row = result.first()
    if row is not None:
        existing: UUID = row[0]
        return existing
    result = await conn.execute(
        text("INSERT INTO profiles (type, user_id) VALUES ('self', :uid) RETURNING id"),
        {"uid": owner_user_id},
    )
    created: UUID = result.scalar_one()
    return created


async def list_profile_ids(
    conn: AsyncConnection, *, user_id: UUID, profile_type: str, limit: int | None = None
) -> list[UUID]:
    """The user's profile ids of one type, oldest first (privileged; the eval slice is stable).

    `limit` takes a fixed, deterministic slice (the dev / CI-gate cut) — ordered by creation then
    id so the same N personas are chosen every run.
    """
    result = await conn.execute(
        text(
            "SELECT id FROM profiles WHERE user_id = :uid AND type = :type "
            "ORDER BY created_at, id LIMIT :limit"
        ),
        {"uid": user_id, "type": profile_type, "limit": limit},
    )
    return [row[0] for row in result]


async def ensure_profile(
    conn: AsyncConnection, profile_id: UUID, *, profile_type: str, user_id: UUID
) -> None:
    """Create a profile with a caller-chosen (deterministic) id; a no-op if it exists.

    The benchmark seed derives stable per-persona ids (uuid5) so re-seeding maps each persona to
    the same profile row.
    """
    await conn.execute(
        text(
            "INSERT INTO profiles (id, type, user_id) VALUES (:id, :type, :uid) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": profile_id, "type": profile_type, "uid": user_id},
    )
