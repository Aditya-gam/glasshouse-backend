"""M0.7 — RBAC require_permission (deny-by-default) + the seeded role/permission matrix.

A self-audit user (no membership) is effectively `owner` → has every permission; a `viewer`
membership is denied write permissions but allowed reads.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.auth.rbac import PERMISSIONS, require_permission
from app.core.config import get_database_settings


@pytest.fixture(scope="module")
def rbac_container() -> Iterator[tuple[PostgresContainer, uuid.UUID, uuid.UUID]]:
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
        with psycopg.connect(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user="glasshouse",
            password="glasshouse",
            dbname="glasshouse",
            autocommit=True,
        ) as conn:

            def one(sql: str, *params: object) -> uuid.UUID:
                row = conn.execute(sql, params).fetchone()
                assert row is not None
                return row[0]  # type: ignore[no-any-return]

            owner_user = one("INSERT INTO users (clerk_user_id) VALUES ('owner') RETURNING id")
            viewer_user = one("INSERT INTO users (clerk_user_id) VALUES ('viewer') RETURNING id")
            org = one(
                "INSERT INTO organizations (clerk_org_id, name) "
                "VALUES ('org_1', 'Org') RETURNING id"
            )
            conn.execute(
                "INSERT INTO memberships (user_id, org_id, role) VALUES (%s, %s, 'viewer')",
                (viewer_user, org),
            )
        yield container, owner_user, viewer_user


@pytest_asyncio.fixture
async def owner_engine(
    rbac_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID],
) -> AsyncIterator[AsyncEngine]:
    container, _, _ = rbac_container
    engine = create_async_engine(container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


async def test_seed_matrix_present(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        perms = (await conn.execute(text("SELECT count(*) FROM permissions"))).scalar_one()
        grants = (await conn.execute(text("SELECT count(*) FROM role_permissions"))).scalar_one()
    assert perms == len(PERMISSIONS)
    assert grants > 0


async def test_self_audit_user_is_owner(
    owner_engine: AsyncEngine, rbac_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID]
) -> None:
    _, owner_user, _ = rbac_container
    dep = require_permission("run:create")
    assert await dep(owner_user, owner_engine) == owner_user


async def test_viewer_denied_write(
    owner_engine: AsyncEngine, rbac_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID]
) -> None:
    _, _, viewer_user = rbac_container
    dep = require_permission("run:create")
    with pytest.raises(HTTPException) as exc:
        await dep(viewer_user, owner_engine)
    assert exc.value.status_code == 403


async def test_viewer_allowed_read(
    owner_engine: AsyncEngine, rbac_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID]
) -> None:
    _, _, viewer_user = rbac_container
    dep = require_permission("inference:read")
    assert await dep(viewer_user, owner_engine) == viewer_user
