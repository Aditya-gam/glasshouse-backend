"""Envelope-crypto helpers (decrypt model A — the DEK never leaves Postgres).

Encryption/decryption of T2 columns happens inside the `SECURITY DEFINER`
`encrypt_field`/`decrypt_field` functions (composed into repository queries); this
module owns the one privileged operation the app issues directly: provisioning a
user's wrapped DEK. The master key is always passed as a bound parameter, never
interpolated — and this module never logs keys or plaintext.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def provision_user_dek(conn: AsyncConnection, user_id: UUID, master_key: str) -> None:
    """Create the user's wrapped DEK. Privileged — run on an owner connection.

    The DEK is generated and wrapped entirely in SQL, so the raw key never enters
    the application process.
    """
    await conn.execute(
        text("SELECT provision_user_dek(:uid, :mk)"),
        {"uid": user_id, "mk": master_key},
    )
