"""Data access for `items` — the only place item SQL lives.

Text is encrypted on the way in (`encrypt_field`) and decrypted on the way out
(`decrypt_field`) inside the query, so plaintext is never written to a column and
the DEK never enters this process. Every statement runs under the caller's RLS
context (see `app.db.rls`), which scopes rows to the owner.
"""

from datetime import datetime
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


async def get_items_text(conn: AsyncConnection, master_key: str) -> list[str]:
    """Decrypted text of the current user's items, oldest first (the tracer's retrieval).

    The embedding/recency Retriever replaces this all-items pass at M1.6.
    """
    result = await conn.execute(
        text("SELECT decrypt_field(owner_user_id, text_ct, :mk) FROM items ORDER BY created_at"),
        {"mk": master_key},
    )
    return [row[0] for row in result]


# --- v2 ingestion persist (M1.3) -------------------------------------------------------------


def _to_pgvector(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal for ``CAST(:embedding AS vector)``."""
    return "[" + ",".join(repr(value) for value in embedding) + "]"


async def insert_canonical_item(
    conn: AsyncConnection,
    *,
    profile_id: UUID,
    owner_user_id: UUID,
    import_source_id: UUID,
    plaintext: str,
    embedding: list[float],
    posted_at: datetime | None,
    original_tz: str | None,
    is_subject_authored: bool,
    master_key: str,
) -> UUID | None:
    """Insert one encrypted + embedded item; return its id, or None if it was a duplicate.

    Text is encrypted in-query (`encrypt_field`, DEK never leaves Postgres); the dedupe key is a
    keyed HMAC and the embedding binds as a pgvector literal. ON CONFLICT on (owner_user_id,
    content_hmac) makes re-imports idempotent (a repeat returns ``None``). Caller sets RLS first.
    """
    result = await conn.execute(
        text(
            "INSERT INTO items (profile_id, owner_user_id, import_source_id, text_ct, "
            "content_hmac, embedding, posted_at, original_tz, is_subject_authored) "
            "VALUES (:profile_id, :owner, :import_source_id, "
            "encrypt_field(:owner, :plaintext, :mk), "
            "encode(hmac(:plaintext, :mk, 'sha256'), 'hex'), "
            "CAST(:embedding AS vector), :posted_at, :original_tz, :is_authored) "
            "ON CONFLICT (owner_user_id, content_hmac) DO NOTHING "
            "RETURNING id"
        ),
        {
            "profile_id": profile_id,
            "owner": owner_user_id,
            "import_source_id": import_source_id,
            "plaintext": plaintext,
            "mk": master_key,
            "embedding": _to_pgvector(embedding),
            "posted_at": posted_at,
            "original_tz": original_tz,
            "is_authored": is_subject_authored,
        },
    )
    row = result.first()
    if row is None:
        return None  # ON CONFLICT DO NOTHING — same content already stored (deduped)
    item_id: UUID = row[0]
    return item_id
