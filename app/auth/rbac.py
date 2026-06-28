"""RBAC — the role→permission matrix + a `require_permission(code)` dependency (rbac.md).

The matrix is the single source of truth here; the 0003 seed migration mirrors it into
`permissions`/`role_permissions` (for the DB/RLS mirror + future enterprise tier). v1 self-audit
users have no membership and are effectively **owner** of their personal scope. Deny-by-default:
a role without the permission → 403.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_current_user, get_owner_engine

# (code, description) — the permission catalogue.
PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("run:create", "Trigger an attack run"),
    ("run:read", "Read runs"),
    ("inference:read", "Read inferences"),
    ("remediation:create", "Trigger a remediation run"),
    ("remediation:read", "Read remediations"),
    ("import:create", "Upload or import data"),
    ("import:read", "Read imports"),
    ("connector:manage", "Link or revoke connectors"),
    ("account:read", "Read the account"),
    ("account:manage", "Manage consents and retention"),
    ("account:export", "Export account data (DSAR)"),
    ("account:erase", "Erase the account"),
    ("eval:read", "Read eval / benchmark results"),
)

_ALL = frozenset(code for code, _ in PERMISSIONS)
_READ = frozenset(
    {"run:read", "inference:read", "remediation:read", "import:read", "account:read", "eval:read"}
)

# role → granted permission codes.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": _ALL,
    "admin": _ALL - {"account:erase"},
    "analyst": _READ | {"run:create", "remediation:create"},
    "viewer": _READ,
}

_DEFAULT_ROLE = "owner"  # v1 self-audit: the user owns their personal scope


async def _resolve_role(engine: AsyncEngine, user_id: UUID) -> str:
    """The user's membership role, or 'owner' (v1 self-audit users have no membership)."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT role FROM memberships WHERE user_id = :uid LIMIT 1"),
            {"uid": user_id},
        )
        row = result.first()
    if row is None:
        return _DEFAULT_ROLE
    role: str = row[0]
    return role


def require_permission(code: str) -> Callable[[UUID, AsyncEngine], Awaitable[UUID]]:
    """A route dependency that authorizes the current user for `code`; 403 if denied."""

    async def dependency(
        user_id: Annotated[UUID, Depends(get_current_user)],
        engine: Annotated[AsyncEngine, Depends(get_owner_engine)],
    ) -> UUID:
        role = await _resolve_role(engine, user_id)
        if code not in ROLE_PERMISSIONS.get(role, frozenset()):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission denied")
        return user_id

    return dependency
