"""M1.9 — POST /v1/runs creates a queued run and enqueues the attack (async lifecycle).

Real Alembic schema (consents + the v2 run tables), app-role + RLS. The arq pool is faked (no
Redis) and records the enqueue; the worker's execution is covered by test_attack_joint. Asserts the
consent gate (fail closed), idempotency, status, and RLS isolation.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_app_engine, get_arq_pool
from app.api.errors import NotFound
from app.api.v1.runs import cancel_run
from app.core.config import get_database_settings
from app.db.rls import set_rls_context
from app.gateway.prompts import ENGINE_VERSION
from app.main import app
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2


class _FakePool:
    """Records enqueue_job calls (no Redis); the endpoint ignores the returned Job."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue_job(
        self, function: str, *args: Any, _job_id: str | None = None, **kwargs: Any
    ) -> object:
        self.jobs.append((function, args, _job_id))
        return object()


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
async def pool() -> _FakePool:
    return _FakePool()


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine, pool: _FakePool) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    app.dependency_overrides[get_arq_pool] = lambda: pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_user(engine: AsyncEngine, *, consented: bool) -> uuid.UUID:
    async with engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        if consented:
            await conn.execute(
                text(
                    "INSERT INTO consents (user_id, purpose, policy_version) "
                    "VALUES (:u, 'self_audit', 'v1')"
                ),
                {"u": user_id},
            )
    return user_id


_ATTACK = {"type": "attack", "params": {}}


async def test_post_creates_queued_run_and_enqueues(
    client: AsyncClient, owner_engine: AsyncEngine, pool: _FakePool
) -> None:
    user = await _seed_user(owner_engine, consented=True)
    resp = await client.post("/v1/runs", json=_ATTACK, headers={"X-Dev-User-Id": str(user)})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    run_id = body["run_id"]
    # the run row exists, queued; the worker job was enqueued with the run + user ids.
    async with owner_engine.connect() as conn:
        status_value = (
            await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
        ).scalar_one()
    assert status_value == "queued"
    assert pool.jobs == [("attack_run", (run_id, str(user)), f"attack:{run_id}")]


async def test_post_without_consent_is_forbidden(
    client: AsyncClient, owner_engine: AsyncEngine, pool: _FakePool
) -> None:
    user = await _seed_user(owner_engine, consented=False)
    resp = await client.post("/v1/runs", json=_ATTACK, headers={"X-Dev-User-Id": str(user)})

    assert resp.status_code == 403
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["type"].endswith("/consent-missing")
    assert pool.jobs == []  # nothing enqueued — fail closed before the queue


async def test_idempotency_key_dedupes(
    client: AsyncClient, owner_engine: AsyncEngine, pool: _FakePool
) -> None:
    user = await _seed_user(owner_engine, consented=True)
    headers = {"X-Dev-User-Id": str(user), "Idempotency-Key": "retry-1"}

    first = await client.post("/v1/runs", json=_ATTACK, headers=headers)
    second = await client.post("/v1/runs", json=_ATTACK, headers=headers)

    assert first.json()["run_id"] == second.json()["run_id"]
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(text("SELECT count(*) FROM runs WHERE idempotency_key = 'retry-1'"))
        ).scalar_one()
    assert count == 1 and len(pool.jobs) == 1  # one run, one enqueue


async def test_get_returns_status(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine, consented=True)
    headers = {"X-Dev-User-Id": str(user)}
    run_id = (await client.post("/v1/runs", json=_ATTACK, headers=headers)).json()["run_id"]

    fetched = await client.get(f"/v1/runs/{run_id}", headers=headers)

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "queued" and fetched.json()["type"] == "attack"


async def test_run_invisible_to_other_user(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user_a = await _seed_user(owner_engine, consented=True)
    user_b = await _seed_user(owner_engine, consented=True)
    run_id = (
        await client.post("/v1/runs", json=_ATTACK, headers={"X-Dev-User-Id": str(user_a)})
    ).json()["run_id"]

    other = await client.get(f"/v1/runs/{run_id}", headers={"X-Dev-User-Id": str(user_b)})

    assert other.status_code == 404  # RLS-hidden → 404, no IDOR signal


async def test_missing_user_is_unauthorized(client: AsyncClient) -> None:
    resp = await client.post("/v1/runs", json=_ATTACK)
    assert resp.status_code == 401


async def test_run_events_streams_status(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine, consented=True)
    headers = {"X-Dev-User-Id": str(user)}
    run_id = (await client.post("/v1/runs", json=_ATTACK, headers=headers)).json()["run_id"]
    async with owner_engine.begin() as conn:  # terminal so the SSE stream emits + closes at once
        await conn.execute(
            text("UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = :r"),
            {"r": run_id},
        )

    resp = await client.get(f"/v1/runs/{run_id}/events", headers=headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in resp.text and "event: done" in resp.text and "succeeded" in resp.text


async def test_run_events_404_for_unknown_run(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    user = await _seed_user(owner_engine, consented=True)
    resp = await client.get(f"/v1/runs/{uuid.uuid4()}/events", headers={"X-Dev-User-Id": str(user)})
    assert resp.status_code == 404


async def test_cancel_run_handler(owner_engine: AsyncEngine, app_engine: AsyncEngine) -> None:
    """Invoke the handler directly (not via ASGI): cancels a queued run; unknown → NotFound."""
    user = await _seed_user(owner_engine, consented=True)
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        profile_id = await get_or_create_self_profile(conn, user)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="queued", engine_version=ENGINE_VERSION
        )
        result = await cancel_run(run_id, conn)
        assert result.status == "canceled"

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user)
        with pytest.raises(NotFound):
            await cancel_run(uuid.uuid4(), conn)


async def test_cancel_marks_queued_run_canceled(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    user = await _seed_user(owner_engine, consented=True)
    headers = {"X-Dev-User-Id": str(user)}
    run_id = (await client.post("/v1/runs", json=_ATTACK, headers=headers)).json()["run_id"]

    resp = await client.post(f"/v1/runs/{run_id}:cancel", headers=headers)

    assert resp.status_code == 202 and resp.json()["status"] == "canceled"
    async with owner_engine.connect() as conn:
        status_value = (
            await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
        ).scalar_one()
    assert status_value == "canceled"


async def test_cancel_unknown_run_is_404(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user = await _seed_user(owner_engine, consented=True)
    resp = await client.post(
        f"/v1/runs/{uuid.uuid4()}:cancel", headers={"X-Dev-User-Id": str(user)}
    )
    assert resp.status_code == 404


async def test_cancel_terminal_run_is_unchanged(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    """Cancelling a run that already finished must return its terminal status, not overwrite it."""
    user = await _seed_user(owner_engine, consented=True)
    headers = {"X-Dev-User-Id": str(user)}
    run_id = (await client.post("/v1/runs", json=_ATTACK, headers=headers)).json()["run_id"]
    async with owner_engine.begin() as conn:  # simulate the worker having finished the run
        await conn.execute(
            text("UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = :r"),
            {"r": run_id},
        )

    resp = await client.post(f"/v1/runs/{run_id}:cancel", headers=headers)

    assert resp.status_code == 202 and resp.json()["status"] == "succeeded"  # guard: not clobbered
