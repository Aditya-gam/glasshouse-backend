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
