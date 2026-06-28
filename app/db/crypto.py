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

from app.core.config import get_crypto_settings


class MasterKeyUnavailableError(RuntimeError):
    """The field-encryption master key is not configured — callers must fail closed."""


def get_master_key() -> str:
    """Return the field-encryption master key (the bound parameter for the crypto functions).

    The KMS-unwrap seam: MVP returns the env ``MASTER_KEY``; in prod the KMS-wrapped master key
    is decrypted in memory here and never stored. Raises if unavailable.
    """
    key = get_crypto_settings().master_key
    if not key:
        raise MasterKeyUnavailableError("MASTER_KEY is not configured")
    return key


async def provision_user_dek(conn: AsyncConnection, user_id: UUID, master_key: str) -> None:
    """Create the user's wrapped DEK. Privileged — run on an owner connection.

    The DEK is generated and wrapped entirely in SQL, so the raw key never enters
    the application process.
    """
    await conn.execute(
        text("SELECT provision_user_dek(:uid, :mk)"),
        {"uid": user_id, "mk": master_key},
    )
