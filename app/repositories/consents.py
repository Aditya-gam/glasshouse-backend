"""Data access for `consents` — the only place consent SQL lives. RLS-scoped to the caller."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def has_active_consent(conn: AsyncConnection, purpose: str) -> bool:
    """True if the caller holds a non-revoked consent row for `purpose` (RLS-scoped to the user)."""
    result = await conn.execute(
        text("SELECT 1 FROM consents WHERE purpose = :purpose AND revoked_at IS NULL LIMIT 1"),
        {"purpose": purpose},
    )
    return result.first() is not None
