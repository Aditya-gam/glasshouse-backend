"""Integration (M1.7a): the joint attack persists canonical inferences + candidates + evidence.

Real v2 schema (migrations incl. the 0005 attribute seed), app-role + RLS. A fake Profiler returns
emission guesses; we assert the normalizer + persistence: Art. 9 values encrypted, non-Art. 9 stored
as JSONB, fabricated evidence refs dropped, reasoning encrypted at rest.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.core.config import get_database_settings
from app.db.crypto import provision_user_dek
from app.db.rls import set_rls_context
from app.domain.output_schema import RawAttributeGuess, RawCandidate, RawEvidence
from app.gateway.prompts import ENGINE_VERSION
from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.geocoding import GeoResolution
from app.services.inference import execute_attack_run, run_text_attack
from app.services.ingestion import ingest_and_persist

_MASTER_KEY = "test-master-key-not-a-real-secret"


class _FakeEmbedder:
    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float((len(t) + i) % 7) for i in range(EMBEDDING_DIM)] for t in texts]


class _FakeDetector:
    def has_identifying_signal(self, text: str) -> bool:
        return False


class _FakeGeocoder:
    """Resolves the two canned places deterministically; everything else misses (no network)."""

    async def resolve(self, place: str) -> GeoResolution | None:
        if "Seattle" in place:
            return GeoResolution(5809844, "city", country="US", region="Washington", city="Seattle")
        if "Portland" in place:
            return GeoResolution(5746545, "city", country="US", region="Oregon", city="Portland")
        if "Porto" in place:
            return GeoResolution(2735943, "city", country="PT", region="Porto", city="Porto")
        return None


class _SequenceProfiler:
    """Returns a different top location per call (2× Seattle, then Portland) to split the runs."""

    def __init__(self, real_item_id: str) -> None:
        self._real = real_item_id
        self._calls = 0

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        self._calls += 1
        city = "Seattle, Washington, US" if self._calls <= 2 else "Portland, Oregon, US"
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[
                    RawCandidate(
                        value_text=city,
                        self_confidence=0.8,
                        evidence=[RawEvidence(ref_id=self._real, quote="walk", rationale="cue")],
                    )
                ],
                reasoning="location cue",
            )
        ]


class _FakeAdapter:
    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


class _FakeProfiler:
    """Returns canned emission guesses; cites a real item id + one fabricated ref."""

    def __init__(self, real_item_id: str) -> None:
        self._real = real_item_id

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[
                    RawCandidate(
                        value_text="Seattle, Washington, US",
                        self_confidence=0.8,
                        evidence=[
                            RawEvidence(
                                ref_id=self._real, quote="Gas Works Park", rationale="park"
                            ),
                            RawEvidence(ref_id="not-a-real-uuid", quote="fabricated"),
                        ],
                    )
                ],
                reasoning="names Seattle-specific places",
            ),
            RawAttributeGuess(
                attribute="birthplace",  # Art. 9 → value + reasoning encrypted
                status="inferred",
                candidates=[RawCandidate(value_text="Porto, Portugal", self_confidence=0.5)],
                reasoning="regional cues",
            ),
            RawAttributeGuess(attribute="sex", status="abstained", candidates=[]),
        ]


@pytest.fixture(scope="module")
def attack_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(attack_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(attack_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(attack_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = attack_container.get_container_host_ip()
    port = attack_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def test_joint_attack_persists_canonical_inferences(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)

    records = [
        ParsedTextRecord(text="My morning walk to Gas Works Park.", is_subject_authored=True)
    ]
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await ingest_and_persist(
            conn,
            _FakeEmbedder(),
            _FakeAdapter(records),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )
    async with owner_engine.connect() as conn:
        item_id: uuid.UUID = (
            await conn.execute(
                text("SELECT id FROM items WHERE owner_user_id = :u"), {"u": user_id}
            )
        ).scalar_one()

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        run_id = await run_text_attack(
            conn,
            _FakeProfiler(str(item_id)),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )

    async with owner_engine.connect() as conn:
        statuses = {
            row[0]: row[1]
            for row in await conn.execute(
                text("SELECT attribute_code, status FROM inferences WHERE run_id = :r"),
                {"r": run_id},
            )
        }
        loc = (
            await conn.execute(
                text(
                    "SELECT c.value, c.value_ct FROM inference_candidates c JOIN inferences i "
                    "ON i.id = c.inference_id WHERE i.attribute_code = 'location' AND i.run_id = :r"
                ),
                {"r": run_id},
            )
        ).one()
        bp = (
            await conn.execute(
                text(
                    "SELECT c.value, c.value_ct, decrypt_field(:u, c.value_ct, :mk) "
                    "FROM inference_candidates c JOIN inferences i ON i.id = c.inference_id "
                    "WHERE i.attribute_code = 'birthplace' AND i.run_id = :r"
                ),
                {"r": run_id, "u": user_id, "mk": _MASTER_KEY},
            )
        ).one()
        loc_evidence = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM inference_evidence e JOIN inference_candidates c "
                    "ON c.id = e.candidate_id JOIN inferences i ON i.id = c.inference_id "
                    "WHERE i.attribute_code = 'location' AND i.run_id = :r"
                ),
                {"r": run_id},
            )
        ).scalar_one()
        loc_reasoning = (
            await conn.execute(
                text(
                    "SELECT decrypt_field(:u, reasoning_ct, :mk) FROM inferences "
                    "WHERE attribute_code = 'location' AND run_id = :r"
                ),
                {"r": run_id, "u": user_id, "mk": _MASTER_KEY},
            )
        ).scalar_one()
        run_status, metrics = (
            (
                await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
            ).scalar_one(),
            (
                await conn.execute(
                    text("SELECT model_calls FROM run_metrics WHERE run_id = :r"), {"r": run_id}
                )
            ).scalar_one(),
        )

    assert (
        run_status == "succeeded" and metrics == 3
    )  # terminal + run_metrics (N=3 calls) persisted
    assert statuses == {"location": "inferred", "birthplace": "inferred", "sex": "abstained"}
    # non-Art. 9 → JSONB plaintext value; Art. 9 (birthplace) → encrypted value_ct, value NULL.
    loc_value, loc_value_ct = loc
    assert loc_value is not None and loc_value_ct is None
    bp_value, bp_value_ct, bp_decrypted = bp
    assert bp_value is None and bp_value_ct is not None
    assert "Porto" in bp_decrypted  # the Art. 9 value round-trips
    # M1.7b: the geocoder enriched both geo_hier values with a resolved GeoNames id.
    loc_payload = loc_value if isinstance(loc_value, dict) else json.loads(loc_value)
    assert loc_payload["geonames_id"] == 5809844 and loc_payload["precision_level"] == "city"
    assert json.loads(bp_decrypted)["geonames_id"] == 2735943  # Art. 9 geo resolves too
    # anti-fabrication: the bogus ref was dropped, only the real item persisted.
    assert loc_evidence == 1
    assert loc_reasoning == "names Seattle-specific places"


async def _seed_user_and_item(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> tuple[uuid.UUID, uuid.UUID]:
    """Provision a user + DEK and persist one item; return (user_id, item_id)."""
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    records = [ParsedTextRecord(text="My morning walk in the city.", is_subject_authored=True)]
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await ingest_and_persist(
            conn,
            _FakeEmbedder(),
            _FakeAdapter(records),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )
    async with owner_engine.connect() as conn:
        item_id: uuid.UUID = (
            await conn.execute(
                text("SELECT id FROM items WHERE owner_user_id = :u"), {"u": user_id}
            )
        ).scalar_one()
    return user_id, item_id


async def test_self_consistency_persists_agreement_fraction(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """N=3 with a split ensemble (2× Seattle, 1× Portland) → top-1 raw 2/3, runner-up 1/3."""
    user_id, item_id = await _seed_user_and_item(owner_engine, app_engine)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        run_id = await run_text_attack(
            conn,
            _SequenceProfiler(str(item_id)),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
            n_runs=3,
        )

    async with owner_engine.connect() as conn:
        candidates = (
            await conn.execute(
                text(
                    "SELECT c.rank, c.raw_confidence, c.confidence_source "
                    "FROM inference_candidates c JOIN inferences i ON i.id = c.inference_id "
                    "WHERE i.attribute_code = 'location' AND i.run_id = :r ORDER BY c.rank"
                ),
                {"r": run_id},
            )
        ).all()

    assert [row.rank for row in candidates] == [1, 2]  # majority + runner-up
    assert all(row.confidence_source == "self_consistency" for row in candidates)
    assert candidates[0].raw_confidence == 2 / 3 and candidates[1].raw_confidence == 1 / 3


async def test_birthplace_skipped_without_special_category_consent(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Art. 9 (birthplace) is inferred only with explicit special-category consent (M1.9)."""
    user_id, item_id = await _seed_user_and_item(owner_engine, app_engine)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="queued", engine_version=ENGINE_VERSION
        )
        await execute_attack_run(
            conn,
            run_id,
            _FakeProfiler(str(item_id)),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
            allow_special_category=False,
        )

    async with owner_engine.connect() as conn:
        attributes = {
            row[0]
            for row in await conn.execute(
                text("SELECT attribute_code FROM inferences WHERE run_id = :r"), {"r": run_id}
            )
        }
    assert "location" in attributes and "birthplace" not in attributes  # Art. 9 skipped
