"""Data access for `users` — the RLS anchor lookup used during authentication."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def get_user_id_by_clerk_id(conn: AsyncConnection, clerk_user_id: str) -> UUID | None:
    """Resolve the internal users.id for a Clerk subject; None if not synced yet."""
    result = await conn.execute(
        text("SELECT id FROM users WHERE clerk_user_id = :cid"),
        {"cid": clerk_user_id},
    )
    row = result.first()
    if row is None:
        return None
    user_id: UUID = row[0]
    return user_id


async def upsert_user(conn: AsyncConnection, clerk_user_id: str, email: str | None) -> None:
    """Insert or update the users mirror for a Clerk subject (idempotent)."""
    await conn.execute(
        text(
            "INSERT INTO users (clerk_user_id, email) VALUES (:cid, :email) "
            "ON CONFLICT (clerk_user_id) DO UPDATE SET email = EXCLUDED.email"
        ),
        {"cid": clerk_user_id, "email": email},
    )


async def delete_user_by_clerk_id(conn: AsyncConnection, clerk_user_id: str) -> None:
    """Delete the user; FK cascades drop owned rows + the DEK (crypto-shred). Idempotent."""
    await conn.execute(
        text("DELETE FROM users WHERE clerk_user_id = :cid"),
        {"cid": clerk_user_id},
    )


async def ensure_user(conn: AsyncConnection, user_id: UUID) -> None:
    """Create a user with a caller-chosen (deterministic) id; a no-op if it exists.

    Used by the benchmark seed for the synthetic-data owner (no Clerk identity).
    """
    await conn.execute(
        text("INSERT INTO users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
        {"id": user_id},
    )
