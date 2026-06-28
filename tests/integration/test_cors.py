"""CORS for the Next.js frontend — allowed origins are echoed, others get no ACAO header."""

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

_ALLOWED = "http://localhost:3000"
_DISALLOWED = "http://evil.example"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


async def test_allowed_origin_is_echoed(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"Origin": _ALLOWED})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == _ALLOWED
    assert resp.headers.get("access-control-allow-credentials") == "true"


async def test_preflight_allows_post_from_origin(client: AsyncClient) -> None:
    resp = await client.options(
        "/v1/runs",
        headers={
            "Origin": _ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key,authorization",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == _ALLOWED


async def test_disallowed_origin_gets_no_cors_header(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"Origin": _DISALLOWED})
    assert resp.status_code == 200  # the request still succeeds; the browser blocks it
    assert "access-control-allow-origin" not in resp.headers
