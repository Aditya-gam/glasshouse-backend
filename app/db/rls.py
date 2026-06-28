"""Row-Level Security context for a scoped session.

After authn, the request's session is scoped to a user by setting the `app.user_id`
GUC; every owned-table policy keys off it. Paired with app-layer scope checks
(defense-in-depth). The value is transaction-local, so call inside a transaction.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def set_rls_context(conn: AsyncConnection, user_id: UUID) -> None:
    """Scope `conn`'s current transaction to `user_id` for RLS (transaction-local)."""
    await conn.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": str(user_id)},
    )
