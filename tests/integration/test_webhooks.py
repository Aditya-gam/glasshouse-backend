"""M0.9 — Svix-verified Clerk webhook → idempotent user sync (signature verified, user synced)."""

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_owner_engine
from app.core.config import get_auth_settings, get_database_settings
from app.main import app
from app.services.webhooks import WebhookError, verify_clerk_webhook

_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _signed_headers(payload: bytes) -> dict[str, str]:
    msg_id, timestamp = "msg_test", str(int(time.time()))
    key = base64.b64decode(_SECRET.removeprefix("whsec_"))
    signature = base64.b64encode(
        hmac.new(key, f"{msg_id}.{timestamp}.{payload.decode()}".encode(), hashlib.sha256).digest()
    ).decode()
    return {"svix-id": msg_id, "svix-timestamp": timestamp, "svix-signature": f"v1,{signature}"}


def _user_event(event_type: str, clerk_user_id: str, email: str | None = None) -> bytes:
    data: dict[str, object] = {"id": clerk_user_id}
    if email is not None:
        data["email_addresses"] = [{"id": "e1", "email_address": email}]
        data["primary_email_address_id"] = "e1"
    return json.dumps({"type": event_type, "data": data}).encode()


@pytest.fixture
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", _SECRET)
    get_auth_settings.cache_clear()


def test_verify_rejects_bad_signature(_configured: None) -> None:
    with pytest.raises(WebhookError):
        verify_clerk_webhook(
            b'{"x":1}', {"svix-id": "m", "svix-timestamp": "1", "svix-signature": "v1,bad"}
        )


@pytest.fixture(scope="module")
def webhook_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        os.environ["DATABASE_URL"] = container.get_connection_url(driver="asyncpg")
        get_database_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            get_database_settings.cache_clear()
        yield container


@pytest_asyncio.fixture
async def client_and_engine(
    webhook_container: PostgresContainer,
) -> AsyncIterator[tuple[AsyncClient, AsyncEngine]]:
    engine = create_async_engine(webhook_container.get_connection_url(driver="asyncpg"))
    # clean the users mirror between tests
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users"))
    app.dependency_overrides[get_owner_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client, engine
    app.dependency_overrides.clear()
    await engine.dispose()


async def _user_exists(engine: AsyncEngine, clerk_user_id: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM users WHERE clerk_user_id = :cid"), {"cid": clerk_user_id}
        )
        return result.first() is not None


async def test_user_created_syncs(
    client_and_engine: tuple[AsyncClient, AsyncEngine], _configured: None
) -> None:
    client, engine = client_and_engine
    payload = _user_event("user.created", "user_new", "a@b.com")
    resp = await client.post("/webhooks/clerk", content=payload, headers=_signed_headers(payload))
    assert resp.status_code == 204
    assert await _user_exists(engine, "user_new")


async def test_user_created_is_idempotent(
    client_and_engine: tuple[AsyncClient, AsyncEngine], _configured: None
) -> None:
    client, engine = client_and_engine
    payload = _user_event("user.created", "user_dup", "a@b.com")
    for _ in range(2):
        resp = await client.post(
            "/webhooks/clerk", content=payload, headers=_signed_headers(payload)
        )
        assert resp.status_code == 204
    async with engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM users WHERE clerk_user_id = 'user_dup'"))
        ).scalar_one()
    assert count == 1


async def test_user_deleted_removes_user(
    client_and_engine: tuple[AsyncClient, AsyncEngine], _configured: None
) -> None:
    client, engine = client_and_engine
    created = _user_event("user.created", "user_gone", "a@b.com")
    await client.post("/webhooks/clerk", content=created, headers=_signed_headers(created))
    deleted = _user_event("user.deleted", "user_gone")
    resp = await client.post("/webhooks/clerk", content=deleted, headers=_signed_headers(deleted))
    assert resp.status_code == 204
    assert not await _user_exists(engine, "user_gone")


async def test_invalid_signature_returns_400(
    client_and_engine: tuple[AsyncClient, AsyncEngine], _configured: None
) -> None:
    client, _ = client_and_engine
    payload = _user_event("user.created", "user_x", "a@b.com")
    bad_headers = {
        "svix-id": "m",
        "svix-timestamp": str(int(time.time())),
        "svix-signature": "v1,bad",
    }
    resp = await client.post("/webhooks/clerk", content=payload, headers=bad_headers)
    assert resp.status_code == 400


async def test_missing_secret_returns_400(
    client_and_engine: tuple[AsyncClient, AsyncEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    get_auth_settings.cache_clear()
    client, _ = client_and_engine
    payload = _user_event("user.created", "user_y", "a@b.com")
    resp = await client.post("/webhooks/clerk", content=payload, headers=_signed_headers(payload))
    assert resp.status_code == 400
    get_auth_settings.cache_clear()
