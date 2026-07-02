"""Data access for `items` — the only place item SQL lives.

Text is encrypted on the way in (`encrypt_field`) and decrypted on the way out
(`decrypt_field`) inside the query, so plaintext is never written to a column and
the DEK never enters this process. Every statement runs under the caller's RLS
context (see `app.db.rls`), which scopes rows to the owner.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


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
    keyed HMAC over (profile, text) — re-importing the same content into the same profile is
    idempotent (a repeat returns ``None``), while the same text under two profiles (distinct
    benchmark personas) stays two items. The embedding binds as a pgvector literal. Caller sets
    RLS first.
    """
    result = await conn.execute(
        text(
            "INSERT INTO items (profile_id, owner_user_id, import_source_id, text_ct, "
            "content_hmac, embedding, posted_at, original_tz, is_subject_authored) "
            "VALUES (:profile_id, :owner, :import_source_id, "
            "encrypt_field(:owner, :plaintext, :mk), "
            "encode(hmac(:dedupe_key, :mk, 'sha256'), 'hex'), "
            "CAST(:embedding AS vector), :posted_at, :original_tz, :is_authored) "
            "ON CONFLICT (owner_user_id, content_hmac) DO NOTHING "
            "RETURNING id"
        ),
        {
            "profile_id": profile_id,
            "owner": owner_user_id,
            "import_source_id": import_source_id,
            "plaintext": plaintext,
            "dedupe_key": f"{profile_id}:{plaintext}",
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


# --- retrieval reads (M1.6) -----------------------------------------------------------------


@dataclass(frozen=True)
class RetrievedItem:
    """One item the Retriever selected: its id + decrypted text (the Profiler's evidence)."""

    id: UUID
    text: str


async def list_items_with_text(
    conn: AsyncConnection, profile_id: UUID, master_key: str
) -> list[RetrievedItem]:
    """The profile's items (id + decrypted text), RLS-scoped on top. Recall-first source."""
    result = await conn.execute(
        text(
            "SELECT id, decrypt_field(owner_user_id, text_ct, :mk) FROM items "
            "WHERE profile_id = :profile_id"
        ),
        {"mk": master_key, "profile_id": profile_id},
    )
    return [RetrievedItem(id=row[0], text=row[1]) for row in result]


async def search_item_ids_by_embedding(
    conn: AsyncConnection, profile_id: UUID, query_embedding: list[float], k: int
) -> list[UUID]:
    """The profile's k item ids most similar to the query vector (pgvector cosine via HNSW)."""
    result = await conn.execute(
        text(
            "SELECT id FROM items WHERE profile_id = :profile_id AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"profile_id": profile_id, "q": _to_pgvector(query_embedding), "k": k},
    )
    return [row[0] for row in result]


async def recent_item_ids(conn: AsyncConnection, profile_id: UUID, n: int) -> list[UUID]:
    """The profile's n most recent item ids (by post time, falling back to ingest time)."""
    result = await conn.execute(
        text(
            "SELECT id FROM items WHERE profile_id = :profile_id "
            "ORDER BY COALESCE(posted_at, created_at) DESC LIMIT :n"
        ),
        {"profile_id": profile_id, "n": n},
    )
    return [row[0] for row in result]
