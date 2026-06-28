"""Request-scoped dependencies.

  - `get_current_user` — verifies the Clerk Bearer JWT (M0.6) and resolves the internal user;
    a non-prod `X-Dev-User-Id` header is the local fallback.
  - `get_scoped_session` — opens a connection on the non-superuser app role, sets the RLS
    context (M0.5), commits at the edge.
  - `get_gateway_client` — the model egress; M1.5 points it at the LiteLLM Proxy.
Fail closed: a missing/invalid user → 401, a missing master key → 500 (never proceed unscoped).
"""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.auth import clerk
from app.core.config import get_app_settings, get_crypto_settings
from app.db.rls import set_rls_context
from app.db.session import app_engine
from app.db.session import engine as owner_engine
from app.gateway.client import GatewayClient
from app.repositories.users import get_user_id_by_clerk_id

_bearer = HTTPBearer(auto_error=False, description="Clerk session JWT")

# The dev header is a non-prod fallback (no Clerk JWT). Honoured ONLY in non-prod environments
# so it can never become a production auth bypass — fail closed everywhere else.
_DEV_AUTH_ENVIRONMENTS = frozenset({"local", "dev", "test"})


def get_app_engine() -> AsyncEngine:
    """The RLS-enforced app-role engine (overridden in tests with the test container engine)."""
    return app_engine


def get_owner_engine() -> AsyncEngine:
    """The owner engine — used to resolve the internal user id before the RLS context is set."""
    return owner_engine


async def _resolve_user(engine: AsyncEngine, clerk_user_id: str) -> UUID:
    async with engine.connect() as conn:
        user_id = await get_user_id_by_clerk_id(conn, clerk_user_id)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown user")
    return user_id


async def get_current_user(
    engine: Annotated[AsyncEngine, Depends(get_owner_engine)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    x_dev_user_id: Annotated[UUID | None, Header()] = None,
) -> UUID:
    """Resolve the request's user — Clerk Bearer JWT, with the dev header as a non-prod fallback."""
    if credentials is not None:
        try:
            claims = clerk.verify_token(credentials.credentials)
        except clerk.AuthError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
            ) from None
        return await _resolve_user(engine, claims.clerk_user_id)

    if get_app_settings().environment in _DEV_AUTH_ENVIRONMENTS and x_dev_user_id is not None:
        return x_dev_user_id
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid credentials"
    )


def get_master_key() -> str:
    """The field-encryption master key; fail closed if it isn't configured."""
    key = get_crypto_settings().master_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="server misconfigured"
        )
    return key


def get_gateway_client() -> GatewayClient:
    """The model egress (Ollama for the tracer; M1.5 → LiteLLM Proxy)."""
    return GatewayClient()


async def get_scoped_session(
    user_id: Annotated[UUID, Depends(get_current_user)],
    engine: Annotated[AsyncEngine, Depends(get_app_engine)],
) -> AsyncIterator[AsyncConnection]:
    """A request-scoped connection bound to the user's RLS context; commits at the edge."""
    async with engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        yield conn
