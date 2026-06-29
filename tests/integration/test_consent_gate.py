"""Integration (M1.10): the consent gate — no run without a valid, non-revoked consent row.

Runs on the real Alembic schema (the `consents` table + its RLS), app-role + RLS-scoped. Proves
the mandatory gate: missing / revoked / another user's consent all block; a valid row allows.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.errors import register_error_handlers
from app.core.config import get_database_settings
from app.db.rls import set_rls_context
from app.repositories import consents as consents_repo
from app.services.consent import (
    ConsentRequiredError,
    has_special_category_consent,
    require_consent,
)


@pytest.fixture(scope="module")
def consent_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(consent_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(consent_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(consent_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = consent_container.get_container_host_ip()
    port = consent_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed_user(engine: AsyncEngine) -> uuid.UUID:
    async with engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        return user_id


async def _grant(
    engine: AsyncEngine,
    user_id: uuid.UUID,
    purpose: str,
    *,
    special: bool = False,
    revoked: bool = False,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO consents (user_id, purpose, special_category, policy_version, "
                "revoked_at) VALUES (:u, :p, :sc, 'v1', :rev)"
            ),
            {
                "u": user_id,
                "p": purpose,
                "sc": special,
                "rev": datetime.now(UTC) if revoked else None,
            },
        )


async def test_no_consent_blocks(owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine)
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        assert await consents_repo.has_active_consent(conn, "self_audit") is False
        with pytest.raises(ConsentRequiredError):
            await require_consent(conn, "self_audit")


async def test_active_consent_allows(owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine)
    await _grant(owner_engine, user, "self_audit")
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        assert await consents_repo.has_active_consent(conn, "self_audit") is True
        await require_consent(conn, "self_audit")  # does not raise


async def test_revoked_consent_blocks(owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine)
    await _grant(owner_engine, user, "self_audit", revoked=True)
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        with pytest.raises(ConsentRequiredError):
            await require_consent(conn, "self_audit")


async def test_consent_is_rls_scoped(owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    user_a = await _seed_user(owner_engine)
    user_b = await _seed_user(owner_engine)
    await _grant(owner_engine, user_a, "self_audit")  # only A consents
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_b)
        with pytest.raises(ConsentRequiredError):
            await require_consent(conn, "self_audit")  # B can't borrow A's consent


async def test_special_category_consent_is_separate(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user = await _seed_user(owner_engine)
    await _grant(owner_engine, user, "art9_inference", special=True)  # Art. 9 only
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        assert await has_special_category_consent(conn) is True
        with pytest.raises(ConsentRequiredError):
            await require_consent(conn, "self_audit")  # the run gate still blocks


async def test_consent_error_maps_to_403_problem_json() -> None:
    """The edge maps the gate's exception to RFC 9457 problem+json (no DB needed)."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise ConsentRequiredError("self_audit")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/boom")

    assert resp.status_code == 403
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"].endswith("/consent-missing") and body["status"] == 403
    assert "self_audit" in body["detail"]
