"""Shared pytest fixtures.

A session-scoped real Postgres (pgvector/pgcrypto image) via testcontainers, plus the
T2 tracer-bullet harness: the schema is applied once as the owning superuser, and tests
run queries as the non-superuser `glasshouse_app` role so RLS is actually enforced
(superusers/owners bypass it).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.db.crypto import provision_user_dek

_PG_IMAGE = "pgvector/pgvector:pg16"
_SCHEMA_SQL = Path(__file__).resolve().parent.parent / "scripts" / "tracer_schema.sql"
_TEST_MASTER_KEY = "test-master-key-not-a-real-secret"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    """A real Postgres for the test session; psycopg drives testcontainers' readiness wait."""
    with PostgresContainer(
        image=_PG_IMAGE,
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        yield container


@pytest_asyncio.fixture
async def engine(postgres: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    """An async engine bound to the test container, with the app's extensions installed."""
    eng = create_async_engine(postgres.get_connection_url(driver="asyncpg"))
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    yield eng
    await eng.dispose()


# --------------------------------------------------------------- T2 harness --


@pytest.fixture(scope="session")
def master_key() -> str:
    return _TEST_MASTER_KEY


@pytest.fixture(scope="session")
def owner_url(postgres: PostgresContainer) -> str:
    """asyncpg URL for the owning superuser (schema + privileged ops)."""
    url: str = postgres.get_connection_url(driver="asyncpg")
    return url


@pytest.fixture(scope="session")
def app_url(postgres: PostgresContainer) -> str:
    """asyncpg URL for the non-superuser application role (RLS-enforced)."""
    host = postgres.get_container_host_ip()
    port = postgres.get_exposed_port(5432)
    return f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"


@pytest.fixture(scope="session")
def _tracer_schema(postgres: PostgresContainer) -> None:
    """Apply the tracer schema once, as the owning superuser.

    Uses a raw asyncpg connection (simple-query protocol) so the multi-statement script —
    dollar-quoted function bodies and all — runs as one script. Own loop via asyncio.run
    to avoid the session-vs-function loop-scope mismatch of async fixtures.
    """
    sql = _SCHEMA_SQL.read_text()

    async def _apply() -> None:
        conn = await asyncpg.connect(
            host=postgres.get_container_host_ip(),
            port=int(postgres.get_exposed_port(5432)),
            user="glasshouse",
            password="glasshouse",
            database="glasshouse",
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_apply())


@pytest_asyncio.fixture
async def owner_engine(owner_url: str, _tracer_schema: None) -> AsyncIterator[AsyncEngine]:
    """Owner engine; truncates the owned tables first so each test starts clean."""
    eng = create_async_engine(owner_url)
    async with eng.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE users, data_keys, items, inferences CASCADE")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def app_engine(app_url: str, _tracer_schema: None) -> AsyncIterator[AsyncEngine]:
    """The application-role engine RLS is enforced against."""
    eng = create_async_engine(app_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def seed_user(owner_engine: AsyncEngine, master_key: str) -> Callable[[], Awaitable[UUID]]:
    """Factory: create a user + provision their wrapped DEK (privileged); return the user id."""

    async def _seed() -> UUID:
        async with owner_engine.begin() as conn:
            result = await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
            user_id: UUID = result.scalar_one()
            await provision_user_dek(conn, user_id, master_key)
        return user_id

    return _seed
