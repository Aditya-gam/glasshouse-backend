"""App-level routes that need no database: liveness and the Scalar API reference."""

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_healthz_ok() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        resp = await test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_scalar_reference_renders() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        resp = await test_client.get("/scalar")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
