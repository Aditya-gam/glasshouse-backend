"""T4 acceptance — POST /v1/runs → infer → GET, end-to-end over HTTP.

CI runs against a real testcontainers Postgres with the gateway faked via dependency_overrides
(no live model). An opt-in test exercises the whole path against real Ollama and skips otherwise.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_app_engine, get_gateway_client, get_master_key
from app.db.rls import set_rls_context
from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.gateway.client import default_gateway_config
from app.main import app
from app.repositories.inferences import get_run_inferences
from app.repositories.items import insert_item

SeedUser = Callable[[], Awaitable[UUID]]

_FAKE_GUESS = RawAttributeGuess(
    attribute="location",
    status="inferred",
    candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.8)],
    reasoning="mentions a Seattle-specific park",
)


class _FakeGateway:
    """Duck-typed gateway returning a canned guess — no live model."""

    def __init__(self, guess: RawAttributeGuess) -> None:
        self._guess = guess

    async def profile_attribute(self, *, system_prompt: str, content: str) -> RawAttributeGuess:
        return self._guess


async def _seed_item(engine: AsyncEngine, user_id: UUID, item_text: str, master_key: str) -> None:
    async with engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await insert_item(conn, user_id, item_text, master_key)


@pytest_asyncio.fixture
async def client(app_engine: AsyncEngine, master_key: str) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    app.dependency_overrides[get_master_key] = lambda: master_key
    app.dependency_overrides[get_gateway_client] = lambda: _FakeGateway(_FAKE_GUESS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def test_post_run_infers_and_get_returns_status(
    client: AsyncClient,
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    seed_user: SeedUser,
    master_key: str,
) -> None:
    user_a = await seed_user()
    await _seed_item(
        app_engine, user_a, "Love my walk to Gas Works Park, PST mornings.", master_key
    )
    headers = {"X-Dev-User-Id": str(user_a)}

    created = await client.post(
        "/v1/runs", json={"type": "attack", "params": {"attribute": "location"}}, headers=headers
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    fetched = await client.get(f"/v1/runs/{run_id}", headers=headers)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "succeeded"
    assert body["type"] == "attack"

    # The inference persisted (read past RLS as the owner; inferences are served by /v1/inferences,
    # which lands at M1.7).
    async with owner_engine.connect() as conn:
        rows = await get_run_inferences(conn, UUID(run_id), master_key)
    assert len(rows) == 1
    assert rows[0].attribute == "location"
    assert rows[0].top_value == "Seattle, WA"
    assert rows[0].reasoning == "mentions a Seattle-specific park"


async def test_idempotency_key_dedupes_run(
    client: AsyncClient,
    app_engine: AsyncEngine,
    owner_engine: AsyncEngine,
    seed_user: SeedUser,
    master_key: str,
) -> None:
    user_a = await seed_user()
    await _seed_item(app_engine, user_a, "Gas Works Park on a PST morning.", master_key)
    headers = {"X-Dev-User-Id": str(user_a), "Idempotency-Key": "retry-abc-123"}
    payload = {"type": "attack", "params": {"attribute": "location"}}

    first = await client.post("/v1/runs", json=payload, headers=headers)
    second = await client.post("/v1/runs", json=payload, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    # The repeated key returns the original run, and no second run was created.
    assert second.json()["run_id"] == first.json()["run_id"]
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM runs WHERE owner_user_id = :u"), {"u": user_a}
            )
        ).scalar_one()
    assert count == 1


async def test_run_invisible_to_other_user(client: AsyncClient, seed_user: SeedUser) -> None:
    user_a = await seed_user()
    user_b = await seed_user()
    created = await client.post(
        "/v1/runs",
        json={"type": "attack", "params": {"attribute": "location"}},
        headers={"X-Dev-User-Id": str(user_a)},
    )
    run_id = created.json()["run_id"]

    other = await client.get(f"/v1/runs/{run_id}", headers={"X-Dev-User-Id": str(user_b)})
    assert other.status_code == 404


async def test_missing_user_header_is_unauthorized(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/runs", json={"type": "attack", "params": {"attribute": "location"}}
    )
    assert resp.status_code == 401


def _ollama_has_model(model: str) -> bool:
    try:
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        names = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return False
    return any(name == model or name.startswith(f"{model}:") for name in names)


@pytest.mark.skipif(
    not _ollama_has_model(default_gateway_config().model),
    reason="requires a running Ollama serving the configured model",
)
async def test_live_end_to_end(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    await _seed_item(
        app_engine, user_a, "I love walking to Gas Works Park before my PST standup.", master_key
    )
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    app.dependency_overrides[get_master_key] = lambda: master_key  # real gateway, not overridden
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as live:
            headers = {"X-Dev-User-Id": str(user_a)}
            created = await live.post(
                "/v1/runs",
                json={"type": "attack", "params": {"attribute": "location"}},
                headers=headers,
            )
            assert created.status_code == 202
            run_id = created.json()["run_id"]
            fetched = await live.get(f"/v1/runs/{run_id}", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert fetched.json()["status"] == "succeeded"
    async with owner_engine.connect() as conn:
        rows = await get_run_inferences(conn, UUID(run_id), master_key)
    assert len(rows) == 1
    assert rows[0].status in {"inferred", "abstained"}
