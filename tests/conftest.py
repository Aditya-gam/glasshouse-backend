"""Shared pytest fixtures.

A session-scoped real Postgres (pgvector/pgcrypto image) via testcontainers for the smoke check.
Each integration module provisions its own Alembic-migrated container + engines + seeding (the RLS
setup lives with the test that needs it). The T2 tracer-schema harness was retired at M1.9b — its
crypto/RLS gates now run on the production schema (test_crypto, test_rls_isolation).
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

_PG_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    """A real Postgres for the session-level smoke check; psycopg drives the readiness wait."""
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
