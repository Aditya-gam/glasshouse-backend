"""Data access for `import_sources` — one data-import event (provenance). SQL lives only here.

RLS-scoped via the profile (`profile_id IN app_owned_profile_ids()`), so a row is only created
under a profile the scoped user owns.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def create_import_source(
    conn: AsyncConnection, profile_id: UUID, *, platform: str, method: str
) -> UUID:
    """Insert one import event (upload/connector/loader) and return its id."""
    result = await conn.execute(
        text(
            "INSERT INTO import_sources (profile_id, platform, method) "
            "VALUES (:profile_id, :platform, :method) RETURNING id"
        ),
        {"profile_id": profile_id, "platform": platform, "method": method},
    )
    import_source_id: UUID = result.scalar_one()
    return import_source_id
