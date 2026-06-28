"""M0.3 acceptance — every v2 model emits valid DDL and all expected tables are created.

Uses a dedicated container so create_all doesn't collide with the T2 tracer schema (which the
shared `postgres` fixture also populates). M0.4's Alembic migration supersedes both.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from app.db.models import Base

_EXPECTED_TABLES = {
    "users",
    "organizations",
    "memberships",
    "permissions",
    "role_permissions",
    "data_keys",
    "consents",
    "profiles",
    "connected_accounts",
    "import_sources",
    "items",
    "media_assets",
    "exif_findings",
    "attributes",
    "runs",
    "inferences",
    "inference_candidates",
    "inference_evidence",
    "run_metrics",
    "eval_labels",
    "eval_results",
    "calibration",
    "remediations",
    "audit_log",
}


@pytest.fixture(scope="module")
def models_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        yield container


@pytest_asyncio.fixture
async def models_engine(models_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(models_container.get_connection_url(driver="asyncpg"))
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_all_v2_tables_created(models_engine: AsyncEngine) -> None:
    async with models_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    missing = _EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {sorted(missing)}"
