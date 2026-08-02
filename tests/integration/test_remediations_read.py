"""Integration (M3.8): GET /v1/remediations — the defend screen reads the persisted frontier.

Real Alembic schema, app-role + RLS. Persists a proven frontier directly, then asserts the endpoint
serves it (status + options + proven after/recovered), synthesizes `cant_break` for an inference
with no options, and is RLS-isolated (another user sees nothing). Content never leaves Postgres.
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
from app.api.errors import NotFound
from app.api.v1.remediations import get_remediation, list_remediations
from app.core.config import get_database_settings
from app.db.crypto import provision_user_dek
from app.db.rls import set_rls_context
from app.domain.output_schema import AttributeGuess, Candidate, Confidence, GeoHierValue
from app.gateway.prompts import ADVERSARY_VERSION
from app.main import app
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.remediations import insert_remediation
from app.repositories.runs import insert_run_v2
from app.services.inference import persist_attribute_guess

_MASTER_KEY = "test-master-key-not-a-real-secret"


@pytest.fixture(scope="module")
def read_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        os.environ["DATABASE_URL"] = container.get_connection_url(driver="asyncpg")
        os.environ["MASTER_KEY"] = _MASTER_KEY
        get_database_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            get_database_settings.cache_clear()
        yield container


@pytest_asyncio.fixture
async def owner_engine(read_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(read_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(read_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = read_container.get_container_host_ip()
    port = read_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr("app.api.v1.remediations.get_master_key", lambda: _MASTER_KEY)
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_inference(app_engine: AsyncEngine, user_id: uuid.UUID) -> uuid.UUID:
    """A location inference (with a value) + a calibrated reliability; returns the inference id."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        attack_run = await insert_run_v2(
            conn, profile_id, run_type="attack", status="succeeded", engine_version="attack_text_v1"
        )
        guess = AttributeGuess(
            attribute="location",
            modality="text",
            status="inferred",
            candidates=[
                Candidate(
                    rank=1,
                    value=GeoHierValue(city="Seattle", country="USA", precision_level="city"),
                    confidence=Confidence(raw=1.0, source="self_consistency"),
                    evidence=[],
                )
            ],
        )
        await persist_attribute_guess(
            conn,
            guess,
            valid_item_ids=set(),
            owner_user_id=user_id,
            profile_id=profile_id,
            run_id=attack_run,
            master_key=_MASTER_KEY,
        )
        inference_id: uuid.UUID = (
            await conn.execute(
                text("SELECT id FROM inferences WHERE run_id = :r AND attribute_code = 'location'"),
                {"r": attack_run},
            )
        ).scalar_one()
        return inference_id


async def _persist_frontier(
    app_engine: AsyncEngine,
    user_id: uuid.UUID,
    inference_id: uuid.UUID,
    confidence_after: float = 0.20,
) -> uuid.UUID:
    """Persist a proven minimal/stronger/remove frontier for a run; returns the run id."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn,
            profile_id,
            run_type="remediation",
            status="succeeded",
            engine_version=ADVERSARY_VERSION,
        )
        for option_key, action in (
            ("minimal", "rewrite"),
            ("stronger", "rewrite"),
            ("remove", "remove"),
        ):
            removed = action == "remove"
            await insert_remediation(
                conn,
                profile_id=profile_id,
                inference_id=inference_id,
                run_id=run_id,
                owner_user_id=user_id,
                master_key=_MASTER_KEY,
                action=action,
                option_key=option_key,
                edited_text=None if removed else "I live near a nearby city",
                span_changes=[
                    {
                        "item_id": "a",
                        "op": "remove_item" if removed else "generalize",
                        "replacement": None if removed else "I live near a nearby city",
                    }
                ],
                misled_value=None,
                confidence_before=0.86,
                confidence_after=confidence_after,
                ci_before={"point": 0.86, "lo": 0.80, "hi": 0.90},
                ci_after={"point": confidence_after, "lo": 0.10, "hi": 0.30},
                significant=True,
                value_recovery_before=True,
                value_recovery_after=False,
                utility_score={"utility_score": 0.75, "meaning": "mostly"},
                is_decoy=False,
                evaluator_engine_version=ADVERSARY_VERSION,
            )
        return run_id


async def _seed_user(owner_engine: AsyncEngine) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    return user_id


async def test_list_by_inference_serves_the_proven_frontier(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)
    await _persist_frontier(app_engine, user_id, inference_id)

    resp = await client.get(
        f"/v1/remediations?inference_id={inference_id}", headers={"X-Dev-User-Id": str(user_id)}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    rem = body[0]
    assert rem["status"] == "proven"
    assert rem["target"]["value"] == "Seattle, USA"
    assert rem["target"]["before"]["point"] == 0.86
    assert [o["key"] for o in rem["options"]] == ["minimal", "stronger", "remove"]
    minimal = rem["options"][0]
    assert minimal["after"]["point"] == 0.20
    assert minimal["recovered"] is False
    assert minimal["utility"] == 75
    assert minimal["edits"][0]["edited"] == "I live near a nearby city"


async def test_get_by_run_id_serves_the_frontier(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)
    run_id = await _persist_frontier(app_engine, user_id, inference_id)

    resp = await client.get(f"/v1/remediations/{run_id}", headers={"X-Dev-User-Id": str(user_id)})

    assert resp.status_code == 200
    assert resp.json()["status"] == "proven"
    assert len(resp.json()["options"]) == 3


async def test_list_returns_one_read_per_run_newest_first(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # re-running remediation for one inference yields one RemediationRead per run, newest first.
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)
    await _persist_frontier(app_engine, user_id, inference_id, confidence_after=0.40)
    newer_run = await _persist_frontier(app_engine, user_id, inference_id, confidence_after=0.15)
    async with owner_engine.begin() as conn:  # make the second run unambiguously the newest
        await conn.execute(
            text(
                "UPDATE remediations SET created_at = now() + interval '1 second' WHERE run_id = :r"
            ),
            {"r": newer_run},
        )

    resp = await client.get(
        f"/v1/remediations?inference_id={inference_id}", headers={"X-Dev-User-Id": str(user_id)}
    )

    body = resp.json()
    assert len(body) == 2  # one RemediationRead per run — the fan-out
    assert body[0]["options"][0]["after"]["point"] == 0.15  # the newest run is first


async def test_inference_without_options_is_cant_break(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)  # no frontier persisted

    resp = await client.get(
        f"/v1/remediations?inference_id={inference_id}", headers={"X-Dev-User-Id": str(user_id)}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "cant_break"
    assert body[0]["options"] == []


async def test_read_is_rls_isolated(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)
    run_id = await _persist_frontier(app_engine, user_id, inference_id)
    other = await _seed_user(owner_engine)

    listed = await client.get(
        f"/v1/remediations?inference_id={inference_id}", headers={"X-Dev-User-Id": str(other)}
    )
    fetched = await client.get(f"/v1/remediations/{run_id}", headers={"X-Dev-User-Id": str(other)})

    assert listed.status_code == 200
    assert listed.json() == []  # inference is RLS-hidden
    assert fetched.status_code == 404  # the run's rows are RLS-hidden → not found


async def test_read_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(f"/v1/remediations/{uuid.uuid4()}")).status_code == 401


async def test_read_handlers_directly(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Invoke the handlers directly (not via ASGI) so the endpoint bodies are coverage-traced."""
    monkeypatch.setattr("app.api.v1.remediations.get_master_key", lambda: _MASTER_KEY)
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)
    run_id = await _persist_frontier(app_engine, user_id, inference_id)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        by_run = await get_remediation(run_id, conn, user_id)
        assert by_run.status == "proven"
        assert len(by_run.options) == 3
        with pytest.raises(NotFound):
            await get_remediation(uuid.uuid4(), conn, user_id)  # unknown run → 404
        by_inference = await list_remediations(conn, user_id, inference_id)
        assert len(by_inference) == 1
        assert by_inference[0].status == "proven"
        assert await list_remediations(conn, user_id, None) == []  # no filter → empty for now


async def test_cant_break_handler_directly(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The synthesized cant_break path (no persisted options) — direct call for coverage."""
    monkeypatch.setattr("app.api.v1.remediations.get_master_key", lambda: _MASTER_KEY)
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_inference(app_engine, user_id)  # no frontier persisted

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        listed = await list_remediations(conn, user_id, inference_id)

    assert len(listed) == 1
    assert listed[0].status == "cant_break"
    assert listed[0].options == []
