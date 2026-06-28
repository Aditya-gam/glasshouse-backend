"""FastAPI application entrypoint."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from scalar_fastapi import get_scalar_api_reference
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import register_error_handlers
from app.api.v1 import (
    account,
    connectors,
    evals,
    imports,
    inferences,
    remediations,
    runs,
    webhooks,
)
from app.api.v1.schemas import Problem
from app.core.config import get_app_settings
from app.db.session import dispose_engines, get_session


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage process-lifetime resources — dispose the DB engine pools on shutdown."""
    yield
    await dispose_engines()


app = FastAPI(title="Glasshouse", lifespan=lifespan)
register_error_handlers(app)

# Every route can surface the one problem+json error shape; documenting it keeps the contract
# conformant (incl. the 501 contract-first stubs). Content type is application/problem+json.
_PROBLEM_RESPONSE = {"model": Problem, "content": {"application/problem+json": {}}}
_COMMON_RESPONSES: dict[int | str, dict[str, object]] = {
    400: _PROBLEM_RESPONSE,
    401: _PROBLEM_RESPONSE,
    404: _PROBLEM_RESPONSE,
    422: _PROBLEM_RESPONSE,
    501: _PROBLEM_RESPONSE,
}
for _router in (
    runs.router,
    imports.router,
    connectors.router,
    inferences.router,
    remediations.router,
    evals.router,
    account.router,
    webhooks.router,
):
    app.include_router(_router, responses=_COMMON_RESPONSES)


@app.get("/scalar", include_in_schema=False)
async def scalar_docs() -> HTMLResponse:
    """Interactive API reference (Scalar), rendered from the OpenAPI schema."""
    return get_scalar_api_reference(
        openapi_url=app.openapi_url or "/openapi.json",
        title=app.title,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — used by the container HEALTHCHECK and the load balancer."""
    settings = get_app_settings()
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}


@app.get("/readyz")
async def readyz(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Readiness probe — verifies the database is reachable."""
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}
