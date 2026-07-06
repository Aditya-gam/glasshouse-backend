"""Integration (M2.5b): GET /v1/inferences — the dashboard cards.

Real Alembic schema, app-role + RLS. Persists inferences directly (the attack pipeline is covered
elsewhere), then asserts the endpoint serves calibrated reliability (never the raw), the taxonomy
severity/sensitivity, Art. 9 value decryption, abstain handling, and RLS isolation.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_app_engine
from app.core.config import get_database_settings
from app.db.crypto import provision_user_dek
from app.db.rls import set_rls_context
from app.domain.output_schema import (
    AttributeGuess,
    Candidate,
    Confidence,
    GeoHierValue,
)
from app.gateway.prompts import ENGINE_VERSION
from app.main import app
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2
from app.services.inference import persist_attribute_guess

_MASTER_KEY = "test-master-key-not-a-real-secret"


@pytest.fixture(scope="module")
def inferences_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(inferences_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(inferences_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(inferences_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = inferences_container.get_container_host_ip()
    port = inferences_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    # the endpoint decrypts the Art.9 value with get_master_key(); pin it to the test key.
    monkeypatch.setattr("app.api.v1.inferences.get_master_key", lambda: _MASTER_KEY)
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_run_with_inferences(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> uuid.UUID:
    """A user with a location (calibrated), an Art.9 birthplace (encrypted+calibrated), an abstained
    age. Returns the user id."""
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="succeeded", engine_version=ENGINE_VERSION
        )
        location = AttributeGuess(
            attribute="location",
            modality="text",
            status="inferred",
            candidates=[
                Candidate(
                    rank=1,
                    value=GeoHierValue(country="Canada", city="Montreal", precision_level="city"),
                    confidence=Confidence(raw=1.0, source="self_consistency"),
                    evidence=[],
                )
            ],
        )
        birthplace = AttributeGuess(
            attribute="birthplace",
            modality="text",
            status="inferred",
            candidates=[
                Candidate(
                    rank=1,
                    value=GeoHierValue(country="France", city="Lyon", precision_level="city"),
                    confidence=Confidence(raw=1.0, source="self_consistency"),
                    evidence=[],
                )
            ],
        )
        age = AttributeGuess(attribute="age", modality="text", status="abstained", candidates=[])
        for guess in (location, birthplace, age):
            await persist_attribute_guess(
                conn,
                guess,
                valid_item_ids=set(),
                owner_user_id=user_id,
                profile_id=profile_id,
                run_id=run_id,
                master_key=_MASTER_KEY,
            )
        # calibrate location + birthplace by writing the candidate reliability directly.
        for attr, rel in (("location", "0.76"), ("birthplace", "0.60")):
            await conn.execute(
                text(
                    "UPDATE inference_candidates SET calibrated_reliability = :rel "
                    "FROM inferences i WHERE inference_candidates.inference_id = i.id "
                    "AND i.attribute_code = :attr AND i.run_id = :run"
                ),
                {"rel": Decimal(rel), "attr": attr, "run": run_id},
            )
    return user_id


async def test_dashboard_serves_calibrated_cards(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_run_with_inferences(owner_engine, app_engine)

    resp = await client.get("/v1/inferences", headers={"X-Dev-User-Id": str(user_id)})

    assert resp.status_code == 200
    body = resp.text
    assert "raw" not in body and "self_consistency" not in body  # raw confidence never serialized
    cards = {card["code"]: card for card in resp.json()}

    location = cards["location"]
    assert location["value"] == "Montreal, Canada"  # rendered from the canonical geo value
    assert location["reliability"] == {"point": 0.76, "lo": 0.76, "hi": 0.76}  # calibrated point
    assert location["severity"] == {"atrisk": "extreme", "jobseeker": "moderate"}  # from taxonomy
    assert location["sensitive"] is False and location["art9"] is False
    assert location["abstain"] is False


async def test_art9_birthplace_value_decrypted_and_flagged(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_run_with_inferences(owner_engine, app_engine)

    cards = {
        c["code"]: c
        for c in (
            await client.get("/v1/inferences", headers={"X-Dev-User-Id": str(user_id)})
        ).json()
    }

    birthplace = cards["birthplace"]
    assert birthplace["value"] == "Lyon, France"  # value_ct decrypted in-query
    assert birthplace["art9"] is True and birthplace["sensitive"] is True
    assert birthplace["reliability"]["point"] == 0.60


async def test_abstained_attribute_has_null_reliability(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_run_with_inferences(owner_engine, app_engine)

    cards = {
        c["code"]: c
        for c in (
            await client.get("/v1/inferences", headers={"X-Dev-User-Id": str(user_id)})
        ).json()
    }

    age = cards["age"]
    assert age["abstain"] is True and age["reliability"] is None and age["value"] is None


async def test_dashboard_is_rls_isolated(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await _seed_run_with_inferences(owner_engine, app_engine)
    async with owner_engine.begin() as conn:
        other = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()

    resp = await client.get("/v1/inferences", headers={"X-Dev-User-Id": str(other)})

    assert resp.status_code == 200 and resp.json() == []  # another user's cards are RLS-hidden


async def _seed_location_run(
    app_engine: AsyncEngine, user_id: uuid.UUID, city: str, country: str
) -> uuid.UUID:
    """A second attack run inferring only location — for the latest-per-attribute dedup test."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="succeeded", engine_version=ENGINE_VERSION
        )
        guess = AttributeGuess(
            attribute="location",
            modality="text",
            status="inferred",
            candidates=[
                Candidate(
                    rank=1,
                    value=GeoHierValue(country=country, city=city, precision_level="city"),
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
            run_id=run_id,
            master_key=_MASTER_KEY,
        )
    return run_id


async def test_dashboard_shows_one_card_per_attribute_latest_run(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # a user who runs the attack more than once must see one card per attribute — the latest.
    user_id = await _seed_run_with_inferences(owner_engine, app_engine)  # location = Montreal
    run2 = await _seed_location_run(app_engine, user_id, "Toronto", "Canada")
    async with owner_engine.begin() as conn:  # make run2 unambiguously the newest
        await conn.execute(
            text(
                "UPDATE inferences SET created_at = now() + interval '1 second' WHERE run_id = :r"
            ),
            {"r": run2},
        )

    cards = (await client.get("/v1/inferences", headers={"X-Dev-User-Id": str(user_id)})).json()

    codes = [card["code"] for card in cards]
    assert len(codes) == len(set(codes))  # exactly one card per attribute — no duplicates
    location = next(card for card in cards if card["code"] == "location")
    assert location["value"] == "Toronto, Canada"  # the latest run wins


async def test_dashboard_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/v1/inferences")).status_code == 401
