"""Integration (M3.7b): the arq `remediation_run` task — gate → prove the frontier → persist rows.

Drives the worker with the real Alembic schema + monkeypatched offline deps (no model, no Redis),
so it exercises the persist-one-row-per-option loop and the decoy consent re-check end to end:
the truthful frontier persists three rows; an opted-in decoy persists a fourth; and a decoy whose
consent is revoked between enqueue and execution degrades to the truthful frontier only.
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
from app.domain.output_schema import (
    AttributeGuess,
    Candidate,
    Confidence,
    GeoHierValue,
    RawAttributeGuess,
    RawCandidate,
)
from app.gateway.client import AnonymizerEdit, DecoyEdit, UtilityVerdict
from app.gateway.prompts import ADVERSARY_VERSION, ENGINE_VERSION
from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.geocoding import GeoResolution
from app.services.inference import persist_attribute_guess
from app.services.ingestion import ingest_and_persist
from app.services.occupation import StringMatchJudge
from app.workers import remediation as remediation_module
from app.workers.remediation import remediation_run

_MASTER_KEY = "test-master-key-not-a-real-secret"
_PIN = "Seattle"


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
        return None


def _recovers(content: str) -> list[RawAttributeGuess]:
    if _PIN in content:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.8)],
            )
        ]
    return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]


class _FakeGatewayClient:
    """Recovers 'Seattle, WA' iff the pin survives; generalizes/decoys it away otherwise."""

    def __init__(self) -> None: ...

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return _recovers(content)

    async def feedback_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return _recovers(content)

    async def anonymize(
        self,
        *,
        text: str,
        spans: list[str],
        attribute: str,
        feedback: str | None = None,
        strength: str = "minimal",
    ) -> AnonymizerEdit:
        return AnonymizerEdit(
            reasoning="generalize",
            operation="generalize",
            edited_text=text.replace(_PIN, "a nearby city"),
            note="ok",
        )

    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: str
    ) -> UtilityVerdict:
        return UtilityVerdict(reasoning="", grade="mostly", confidence=0.9)

    async def decoy(
        self, *, text: str, spans: list[str], attribute: str, decoy_value: str | None = None
    ) -> DecoyEdit:
        return DecoyEdit(
            reasoning="inject false cue",
            edited_text=text.replace(_PIN, "Portland"),
            decoy_value="Portland, OR",
            note="implies a different city",
        )


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
    monkeypatch.setattr(remediation_module, "app_engine", app_engine)
    monkeypatch.setattr(remediation_module, "default_embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(remediation_module, "default_pii_detector", lambda: _FakeDetector())
    monkeypatch.setattr(remediation_module, "default_geocoder", lambda: _FakeGeocoder())
    monkeypatch.setattr(
        remediation_module, "GatewayOccupationJudge", lambda gateway: StringMatchJudge()
    )
    monkeypatch.setattr(remediation_module, "GatewayClient", _FakeGatewayClient)
    monkeypatch.setattr("app.db.crypto.get_master_key", lambda: _MASTER_KEY)


async def _seed(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, *, decoy_consent: bool = False
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A user (+DEK, +consents, +a Seattle item, +a location inference, +a queued remediation run).

    Returns (user_id, remediation_run_id, inference_id).
    """
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
        await conn.execute(
            text(
                "INSERT INTO consents (user_id, purpose, policy_version) "
                "VALUES (:u, 'self_audit', 'v1')"
            ),
            {"u": user_id},
        )
        if decoy_consent:
            await conn.execute(
                text(
                    "INSERT INTO consents (user_id, purpose, policy_version) "
                    "VALUES (:u, 'decoy', 'v1')"
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
        attack_run = await insert_run_v2(
            conn, profile_id, run_type="attack", status="succeeded", engine_version=ENGINE_VERSION
        )
        guess = AttributeGuess(
            attribute="location",
            modality="text",
            status="inferred",
            candidates=[
                Candidate(
                    rank=1,
                    value=GeoHierValue(
                        city="Seattle", region="WA", country="USA", precision_level="city"
                    ),
                    confidence=Confidence(raw=0.9, source="self_consistency"),
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
        remediation_run = await insert_run_v2(
            conn,
            profile_id,
            run_type="remediation",
            status="queued",
            engine_version=ADVERSARY_VERSION,
        )
    return user_id, remediation_run, inference_id


async def _options(owner_engine: AsyncEngine, run_id: uuid.UUID) -> dict[str, bool]:
    """The persisted frontier for a run: {option_key: is_decoy}."""
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT option_key, is_decoy FROM remediations WHERE run_id = :r"), {"r": run_id}
        )
        return {row[0]: row[1] for row in rows}


@pytest.mark.usefixtures("patched_worker")
async def test_remediation_run_persists_the_truthful_frontier(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id, run_id, inference_id = await _seed(owner_engine, app_engine)

    await remediation_run({}, str(run_id), str(user_id), str(inference_id), False)

    options = await _options(owner_engine, run_id)
    assert set(options) == {"minimal", "stronger", "remove"}  # one row per truthful option
    assert not any(options.values())  # none is a decoy


@pytest.mark.usefixtures("patched_worker")
async def test_remediation_run_with_decoy_persists_the_decoy_row(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id, run_id, inference_id = await _seed(owner_engine, app_engine, decoy_consent=True)

    await remediation_run({}, str(run_id), str(user_id), str(inference_id), True)

    options = await _options(owner_engine, run_id)
    assert set(options) == {"minimal", "stronger", "remove", "decoy"}
    assert options["decoy"] is True  # the opt-in decoy row is flagged
    async with owner_engine.connect() as conn:
        misled = (
            await conn.execute(
                text("SELECT misled_value FROM remediations WHERE run_id = :r AND is_decoy"),
                {"r": run_id},
            )
        ).scalar_one()
    assert misled == "Portland, OR"  # the plausible false value the adversary is steered to


@pytest.mark.usefixtures("patched_worker")
async def test_decoy_revoked_between_enqueue_and_execution_degrades_to_truthful(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # enqueued with decoy=True, but no standing decoy consent at execution → truthful frontier only.
    user_id, run_id, inference_id = await _seed(owner_engine, app_engine, decoy_consent=False)

    await remediation_run({}, str(run_id), str(user_id), str(inference_id), True)

    options = await _options(owner_engine, run_id)
    assert "decoy" not in options  # fail closed — no decoy row without live consent
    assert set(options) == {"minimal", "stronger", "remove"}


@pytest.mark.usefixtures("patched_worker")
async def test_remediation_run_blocked_when_consent_revoked(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id, run_id, inference_id = await _seed(owner_engine, app_engine)
    async with owner_engine.begin() as conn:  # self_audit revoked before pickup
        await conn.execute(
            text("UPDATE consents SET revoked_at = now() WHERE user_id = :u"), {"u": user_id}
        )

    await remediation_run({}, str(run_id), str(user_id), str(inference_id), False)

    async with owner_engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
        ).scalar_one()
        count = (
            await conn.execute(
                text("SELECT count(*) FROM remediations WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
    assert status == "failed" and count == 0  # terminal, no rows, does not raise
