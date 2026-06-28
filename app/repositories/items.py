"""Data access for `items` — the only place item SQL lives.

Text is encrypted on the way in (`encrypt_field`) and decrypted on the way out
(`decrypt_field`) inside the query, so plaintext is never written to a column and
the DEK never enters this process. Every statement runs under the caller's RLS
context (see `app.db.rls`), which scopes rows to the owner.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_item(
    conn: AsyncConnection, owner_user_id: UUID, plaintext: str, master_key: str
) -> UUID:
    """Insert one encrypted item; return its id. Caller sets the RLS context first."""
    result = await conn.execute(
        text(
            "INSERT INTO items (owner_user_id, text_ct, content_hmac) "
            "VALUES (:owner, encrypt_field(:owner, :plaintext, :mk), "
            "        encode(hmac(:plaintext, :mk, 'sha256'), 'hex')) "
            "RETURNING id"
        ),
        {"owner": owner_user_id, "plaintext": plaintext, "mk": master_key},
    )
    item_id: UUID = result.scalar_one()
    return item_id


async def get_item_text(conn: AsyncConnection, item_id: UUID, master_key: str) -> str | None:
    """Return the decrypted text, or None if the row is absent or RLS-hidden."""
    result = await conn.execute(
        text("SELECT decrypt_field(owner_user_id, text_ct, :mk) FROM items WHERE id = :id"),
        {"id": item_id, "mk": master_key},
    )
    row = result.first()
    if row is None:
        return None
    plaintext: str = row[0]
    return plaintext


async def list_item_ids(conn: AsyncConnection) -> list[UUID]:
    """All item ids visible under the current RLS context (empty if unscoped)."""
    result = await conn.execute(text("SELECT id FROM items ORDER BY created_at"))
    return [row[0] for row in result]
