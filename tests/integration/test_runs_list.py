"""Integration (M5.2): GET /v1/runs — cursor-paginated, RLS-scoped run list.

Real Alembic schema, app-role + RLS. Seeds runs with increasing created_at, walks the cursor pages,
and asserts newest-first order with no gaps/dupes, RLS scoping, an empty list, and a 422 on a bad
cursor. Keyset (created_at, id), never OFFSET.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_app_engine
from app.api.v1.runs import list_runs
from app.core.config import get_database_settings
from app.db.rls import set_rls_context
from app.main import app
from app.repositories.profiles import get_or_create_self_profile


@pytest.fixture(scope="module")
def runs_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(runs_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(runs_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(runs_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = runs_container.get_container_host_ip()
    port = runs_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id)}


async def _seed_user(owner_engine: AsyncEngine) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
    return user_id


async def _seed_runs(app_engine: AsyncEngine, user_id: uuid.UUID, n: int) -> list[uuid.UUID]:
    """Seed `n` runs with strictly increasing created_at; returns ids oldest-first."""
    ids: list[uuid.UUID] = []
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        for i in range(n):
            run_id: uuid.UUID = (
                await conn.execute(
                    text(
                        "INSERT INTO runs (profile_id, type, status, engine_version, created_at) "
                        "VALUES (:p, 'attack', 'queued', 'attack_text_v1', "
                        "        now() + (:i * interval '1 second')) RETURNING id"
                    ),
                    {"p": profile_id, "i": i},
                )
            ).scalar_one()
            ids.append(run_id)
    return ids


async def test_paginates_newest_first_without_gaps_or_dupes(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    ids = await _seed_runs(app_engine, user_id, 5)  # oldest-first
    newest_first = [str(run_id) for run_id in reversed(ids)]

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        url = "/v1/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
        resp = await client.get(url, headers=_headers(user_id))
        assert resp.status_code == 200
        body = resp.json()
        collected += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10  # guard against a cursor that never terminates

    assert collected == newest_first  # every run once, newest first, across page boundaries
    assert pages == 3  # 2 + 2 + 1


async def test_pagination_tiebreaks_on_id_for_equal_created_at(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # three runs sharing ONE created_at — only the (created_at, id) tiebreak keeps the page walk
    # whole (a `created_at < :ts` keyset would silently drop the boundary row). now() is the txn
    # start time, so same-transaction inserts naturally collide on created_at.
    user_id = await _seed_user(owner_engine)
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        ids = [
            (
                await conn.execute(
                    text(
                        "INSERT INTO runs (profile_id, type, status, engine_version, created_at) "
                        "VALUES (:p, 'attack', 'queued', 'attack_text_v1', "
                        "        TIMESTAMPTZ '2026-05-01 00:00:00+00') RETURNING id"
                    ),
                    {"p": profile_id},
                )
            ).scalar_one()
            for _ in range(3)
        ]

    collected: list[str] = []
    cursor: str | None = None
    while True:
        url = "/v1/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url, headers=_headers(user_id))).json()
        collected += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    # all three emitted exactly once despite the identical timestamp — the id tiebreak held
    assert sorted(collected) == sorted(str(run_id) for run_id in ids)


async def test_runs_list_is_rls_scoped_to_the_caller(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_a = await _seed_user(owner_engine)
    user_b = await _seed_user(owner_engine)
    a_ids = await _seed_runs(app_engine, user_a, 2)
    await _seed_runs(app_engine, user_b, 3)

    resp = await client.get("/v1/runs?limit=50", headers=_headers(user_a))

    returned = {item["id"] for item in resp.json()["items"]}
    assert returned == {str(run_id) for run_id in a_ids}  # only A's runs, never B's


async def test_empty_list_has_a_null_cursor(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user_id = await _seed_user(owner_engine)

    resp = await client.get("/v1/runs", headers=_headers(user_id))

    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_malformed_cursor_is_422(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user_id = await _seed_user(owner_engine)

    resp = await client.get("/v1/runs?cursor=not-a-valid-cursor%21%21", headers=_headers(user_id))

    assert resp.status_code == 422


async def test_list_handler_directly_for_coverage(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # the endpoint body runs under httpx-ASGI (untraced by CI coverage); call it directly too.
    user_id = await _seed_user(owner_engine)
    ids = await _seed_runs(app_engine, user_id, 3)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        page = await list_runs(conn, limit=2)

    assert [item.id for item in page.items] == [ids[-1], ids[-2]]  # newest two
    assert page.next_cursor is not None
