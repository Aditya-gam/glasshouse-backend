"""FastAPI application entrypoint."""

from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session

app = FastAPI(title="Glasshouse")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — used by the container HEALTHCHECK and the load balancer."""
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}


@app.get("/readyz")
async def readyz(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Readiness probe — verifies the database is reachable."""
    await session.execute(text("SELECT 1"))
    return {"status": "ready"}
