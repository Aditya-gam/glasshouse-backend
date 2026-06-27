"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.core.config import get_settings

app = FastAPI(title="Inference Exposure Auditor")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — used by the container HEALTHCHECK and the load balancer."""
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}
