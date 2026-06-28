"""Shared pytest fixtures.

R1 ships a minimal harness: a session-scoped, real Postgres (pgvector/pgcrypto image)
via testcontainers. The fuller harness — per-test transaction rollback, RLS GUCs, and the
SynthPAI fixtures — arrives with T2/M0.5.
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
