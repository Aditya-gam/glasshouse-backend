"""Request-scoped dependencies.

These are deliberate seams that later milestones harden:
  - `get_current_user` — a dev header now; M0.6 swaps the body for Clerk JWT verification.
  - `get_scoped_session` — opens a connection on the non-superuser app role, sets the RLS
    context, commits at the edge; M0.5 formalizes this as middleware.
  - `get_gateway_client` — the model egress; M1.5 points it at the LiteLLM Proxy.
Fail closed: a missing user → 401, a missing master key → 500 (never proceed unscoped).
"""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import get_app_settings, get_crypto_settings
from app.db.rls import set_rls_context
from app.db.session import app_engine
from app.gateway.client import GatewayClient

# The dev header is a stand-in until Clerk (M0.6). Honour it ONLY in non-prod environments so
# it can never become a production auth bypass — fail closed everywhere else.
_DEV_AUTH_ENVIRONMENTS = frozenset({"local", "dev", "test"})


def get_current_user(x_dev_user_id: Annotated[UUID | None, Header()] = None) -> UUID:
    """Dev auth seam: the user id comes from the `X-Dev-User-Id` header (M0.6 → Clerk JWT)."""
    if get_app_settings().environment not in _DEV_AUTH_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication not configured"
        )
    if x_dev_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing user")
    return x_dev_user_id


def get_app_engine() -> AsyncEngine:
    """The RLS-enforced app-role engine (overridden in tests with the test container engine)."""
    return app_engine


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
