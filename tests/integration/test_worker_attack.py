"""Integration (M1.9): the arq `attack_run` task — gate → build deps → execute → persist.

Drives the worker task with the real Alembic schema and monkeypatched dependencies (no model, no
Redis): the happy path succeeds, a revoked consent marks the run failed without raising, and a
gateway failure marks it failed and re-raises (so arq retries / dead-letters).
"""

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
from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.gateway.prompts import ENGINE_VERSION
from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.repositories import runs as runs_repo
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.geocoding import GeoResolution
from app.services.inference import execute_attack_run
from app.services.ingestion import ingest_and_persist
from app.services.occupation import StringMatchJudge
from app.workers import attack as attack_module
from app.workers.attack import attack_run

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
    async def resolve(self, place: str) -> GeoResolution | None:
        return None  # heuristic split is kept


class _FakeGatewayClient:
    def __init__(self) -> None: ...

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.8)],
                reasoning="cue",
            )
        ]


class _FailingGatewayClient:
    def __init__(self) -> None: ...

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        raise RuntimeError("model down")


class _FakeAdapter:
    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


@pytest.fixture(scope="module")
def worker_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(worker_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(worker_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(worker_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = worker_container.get_container_host_ip()
    port = worker_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest.fixture
def patched_worker(monkeypatch: pytest.MonkeyPatch, app_engine: AsyncEngine) -> None:
    """Point the worker at the test engine + fake (offline) dependencies."""
    monkeypatch.setattr(attack_module, "app_engine", app_engine)
    monkeypatch.setattr(attack_module, "default_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(attack_module, "default_pii_detector", lambda: _FakeDetector())
    monkeypatch.setattr(attack_module, "default_geocoder", lambda: _FakeGeocoder())
    monkeypatch.setattr(attack_module, "GatewayOccupationJudge", lambda gateway: StringMatchJudge())
    monkeypatch.setattr("app.db.crypto.get_master_key", lambda: _MASTER_KEY)


async def _seed(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, *, consented: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    """Provision a user (+DEK, +item, +queued run) and optionally consent; return (user, run)."""
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
        if consented:
            await conn.execute(
                text(
                    "INSERT INTO consents (user_id, purpose, policy_version) "
                    "VALUES (:u, 'self_audit', 'v1')"
                ),
                {"u": user_id},
            )
    records = [ParsedTextRecord(text="A PST morning walk in Seattle.", is_subject_authored=True)]
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await ingest_and_persist(
            conn,
            _FakeEmbedder(),
            _FakeAdapter(records),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="queued", engine_version=ENGINE_VERSION
        )
    return user_id, run_id


async def _run_status(owner_engine: AsyncEngine, run_id: uuid.UUID) -> str:
    async with owner_engine.connect() as conn:
        status: str = (
            await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
        ).scalar_one()
    return status


@pytest.mark.usefixtures("patched_worker")
async def test_attack_run_executes_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(attack_module, "GatewayClient", _FakeGatewayClient)
    user_id, run_id = await _seed(owner_engine, app_engine, consented=True)

    await attack_run({}, str(run_id), str(user_id))

    async with owner_engine.connect() as conn:
        attributes = {
            row[0]
            for row in await conn.execute(
                text("SELECT attribute_code FROM inferences WHERE run_id = :r"), {"r": run_id}
            )
        }
    assert await _run_status(owner_engine, run_id) == "succeeded" and "location" in attributes


@pytest.mark.usefixtures("patched_worker")
async def test_attack_run_blocked_when_consent_revoked(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(attack_module, "GatewayClient", _FakeGatewayClient)
    user_id, run_id = await _seed(owner_engine, app_engine, consented=False)

    await attack_run({}, str(run_id), str(user_id))  # no consent → terminal, does not raise

    assert await _run_status(owner_engine, run_id) == "failed"


@pytest.mark.usefixtures("patched_worker")
async def test_attack_run_failure_marks_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(attack_module, "GatewayClient", _FailingGatewayClient)
    user_id, run_id = await _seed(owner_engine, app_engine, consented=True)

    with pytest.raises(RuntimeError):
        await attack_run({}, str(run_id), str(user_id))  # re-raises for arq retry/DLQ

    assert await _run_status(owner_engine, run_id) == "failed"


@pytest.mark.usefixtures("patched_worker")
async def test_attack_run_skips_canceled_run(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(attack_module, "GatewayClient", _FakeGatewayClient)
    user_id, run_id = await _seed(owner_engine, app_engine, consented=True)
    async with owner_engine.begin() as conn:  # canceled before the worker picks it up
        await conn.execute(text("UPDATE runs SET status = 'canceled' WHERE id = :r"), {"r": run_id})

    await attack_run({}, str(run_id), str(user_id))  # honors the cancellation, does nothing

    async with owner_engine.connect() as conn:
        inferences = (
            await conn.execute(
                text("SELECT count(*) FROM inferences WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
    assert await _run_status(owner_engine, run_id) == "canceled" and inferences == 0


@pytest.mark.usefixtures("patched_worker")
async def test_cancel_does_not_clobber_a_succeeded_run(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """A cancel that lands after the run finished must not overwrite the terminal status."""
    monkeypatch.setattr(attack_module, "GatewayClient", _FakeGatewayClient)
    user_id, run_id = await _seed(owner_engine, app_engine, consented=True)
    await attack_run({}, str(run_id), str(user_id))
    assert await _run_status(owner_engine, run_id) == "succeeded"

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        applied = await runs_repo.set_run_status_where(
            conn, run_id, "canceled", allowed_from=("queued", "running"), finished=True
        )
    assert applied is False and await _run_status(owner_engine, run_id) == "succeeded"


@pytest.mark.usefixtures("patched_worker")
async def test_execute_attack_run_skips_a_non_queued_run(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """The claim (queued→running) fails on a non-queued run → nothing runs or persists."""
    user_id, run_id = await _seed(owner_engine, app_engine, consented=True)
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE runs SET status = 'canceled' WHERE id = :r"), {"r": run_id})

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await execute_attack_run(
            conn,
            run_id,
            _FakeGatewayClient(),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
            allow_special_category=True,
        )

    async with owner_engine.connect() as conn:
        inferences = (
            await conn.execute(
                text("SELECT count(*) FROM inferences WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
    assert await _run_status(owner_engine, run_id) == "canceled" and inferences == 0


@pytest.mark.usefixtures("patched_worker")
async def test_attack_run_is_a_noop_for_a_missing_run(
    monkeypatch: pytest.MonkeyPatch, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    monkeypatch.setattr(attack_module, "GatewayClient", _FakeGatewayClient)
    user_id, _ = await _seed(owner_engine, app_engine, consented=True)

    await attack_run({}, str(uuid.uuid4()), str(user_id))  # unknown run → guard returns, no error
