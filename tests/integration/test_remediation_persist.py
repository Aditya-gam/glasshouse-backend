"""Integration (M3.7): the `remediations` insert — crypto round-trip + RLS isolation.

Real Alembic schema, app-role + RLS. Persists a proven remediation with an encrypted suggested
rewrite, then asserts the edit round-trips through the owner's DEK and that another user cannot see
the row (the two mandatory gates: crypto round-trip + RLS). Content plaintext never leaves Postgres.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

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
)
from app.gateway.client import AnonymizerEdit, UtilityVerdict
from app.gateway.prompts import ADVERSARY_VERSION
from app.repositories.calibration import upsert_calibration_bucket
from app.repositories.inferences import insert_inference_v2
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.remediations import insert_remediation
from app.repositories.runs import insert_run_v2
from app.services.consent import ConsentRequiredError
from app.services.inference import persist_attribute_guess
from app.services.remediation import execute_remediation_run, resolve_floor_margin

_MASTER_KEY = "test-master-key-not-a-real-secret"
_EDIT = "I live near a nearby city"  # the suggested rewrite (T2, encrypted at rest)


class _UnusedGateway:
    """A gateway whose slots must NOT be reached (the early-exit paths never call the model)."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        raise AssertionError("the adversary must not run on an early-exit path")

    async def feedback_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        raise AssertionError("the feedback adversary must not run on an early-exit path")

    async def anonymize(
        self, *, text: str, spans: list[str], attribute: str, feedback: str | None = None
    ) -> AnonymizerEdit:
        raise AssertionError("the anonymizer must not run on an early-exit path")

    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: str
    ) -> UtilityVerdict:
        raise AssertionError("the utility judge must not run on an early-exit path")


class _UnusedEmbedder:
    @property
    def dimension(self) -> int:
        return 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("retrieval must not run on an early-exit path")


class _UnusedPii:
    def has_identifying_signal(self, text: str) -> bool:
        raise AssertionError("retrieval must not run on an early-exit path")


class _UnusedGeocoder:
    async def resolve(self, place: str) -> None:
        raise AssertionError("geocoding must not run on an early-exit path")


async def _seed_inference(
    app_engine: AsyncEngine, user_id: uuid.UUID, guess: AttributeGuess
) -> tuple[uuid.UUID, uuid.UUID]:
    """Persist a remediation run + one inference from `guess`; returns (run_id, inference_id)."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn,
            profile_id,
            run_type="remediation",
            status="queued",
            engine_version=ADVERSARY_VERSION,
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
        inference_id = (
            await conn.execute(
                text("SELECT id FROM inferences WHERE run_id = :r AND attribute_code = :a"),
                {"r": run_id, "a": guess.attribute},
            )
        ).scalar_one()
        return run_id, inference_id


async def _run_status(app_engine: AsyncEngine, user_id: uuid.UUID, run_id: uuid.UUID) -> str:
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        status: str = (
            await conn.execute(text("SELECT status FROM runs WHERE id = :r"), {"r": run_id})
        ).scalar_one()
        return status


async def _remediation_count(app_engine: AsyncEngine, user_id: uuid.UUID, run_id: uuid.UUID) -> int:
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        count: int = (
            await conn.execute(
                text("SELECT count(*) FROM remediations WHERE run_id = :r"), {"r": run_id}
            )
        ).scalar_one()
        return count


@pytest.fixture(scope="module")
def remediation_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(remediation_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(remediation_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(remediation_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = remediation_container.get_container_host_ip()
    port = remediation_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed_user(owner_engine: AsyncEngine) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    return user_id


async def _seed_remediation(app_engine: AsyncEngine, user_id: uuid.UUID) -> uuid.UUID:
    """A proven remediation for one location inference; returns the remediation id."""
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
        inference_id = await insert_inference_v2(
            conn,
            run_id=run_id,
            profile_id=profile_id,
            owner_user_id=user_id,
            attribute_code="location",
            status="inferred",
            engine_version=ADVERSARY_VERSION,
            reasoning=None,
            reasoning_reveals_art9=False,
            master_key=_MASTER_KEY,
        )
        return await insert_remediation(
            conn,
            profile_id=profile_id,
            inference_id=inference_id,
            run_id=run_id,
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
            action="rewrite",
            edited_text=_EDIT,
            span_changes=[{"item_id": "a", "op": "generalize", "replacement": _EDIT}],
            confidence_before=0.86,
            confidence_after=0.21,
            ci_before={"point": 0.86, "lo": 0.80, "hi": 0.90},
            ci_after={"point": 0.21, "lo": 0.15, "hi": 0.28},
            significant=True,
            value_recovery_before=True,
            value_recovery_after=False,
            utility_score={"utility_score": 0.75, "meaning": "mostly", "passes_floor": True},
            is_decoy=False,
            evaluator_engine_version=ADVERSARY_VERSION,
        )


async def test_remediation_round_trips_the_encrypted_edit(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    remediation_id = await _seed_remediation(app_engine, user_id)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        row = (
            await conn.execute(
                text(
                    "SELECT decrypt_field(app_user_id(), edited_text_ct, :mk)::text, action, "
                    "  significant, value_recovery_before, value_recovery_after, "
                    "  confidence_before, confidence_after, span_changes "
                    "FROM remediations WHERE id = :id"
                ),
                {"mk": _MASTER_KEY, "id": remediation_id},
            )
        ).one()

    assert row[0] == _EDIT  # the suggested edit decrypts with the owner's DEK (crypto round-trip)
    assert row[1] == "rewrite" and row[2] is True  # proven, significant
    assert row[3] is True and row[4] is False  # the value-recovery flip persisted
    assert float(row[5]) == 0.86 and float(row[6]) == 0.21  # calibrated before/after
    assert row[7] == [{"item_id": "a", "op": "generalize", "replacement": _EDIT}]  # span_changes


async def test_remediation_is_rls_isolated(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    remediation_id = await _seed_remediation(app_engine, user_id)
    other = await _seed_user(owner_engine)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, other)  # a different user
        visible = (
            await conn.execute(
                text("SELECT count(*) FROM remediations WHERE id = :id"), {"id": remediation_id}
            )
        ).scalar_one()

    assert visible == 0  # another user's remediation is RLS-hidden


async def test_art9_target_without_consent_fails_closed(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # remediating birthplace (Art. 9) re-infers a special category — deny without art9 consent.
    user_id = await _seed_user(owner_engine)
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
    run_id, inference_id = await _seed_inference(app_engine, user_id, birthplace)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        with pytest.raises(ConsentRequiredError):
            await execute_remediation_run(
                conn,
                run_id,
                _UnusedGateway(),
                _UnusedEmbedder(),
                _UnusedPii(),
                _UnusedGeocoder(),
                owner_user_id=user_id,
                master_key=_MASTER_KEY,
                target_inference_id=inference_id,
                allow_special_category=False,  # art9 consent absent → fail closed before any model
            )


async def test_abstained_target_succeeds_with_no_row(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # an abstained inference has no value to defend → succeed with no row (honest, no false safety).
    user_id = await _seed_user(owner_engine)
    abstained = AttributeGuess(attribute="age", modality="text", status="abstained", candidates=[])
    run_id, inference_id = await _seed_inference(app_engine, user_id, abstained)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await execute_remediation_run(
            conn,
            run_id,
            _UnusedGateway(),
            _UnusedEmbedder(),
            _UnusedPii(),
            _UnusedGeocoder(),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
            target_inference_id=inference_id,
            allow_special_category=True,
        )

    assert await _run_status(app_engine, user_id, run_id) == "succeeded"
    assert await _remediation_count(app_engine, user_id, run_id) == 0  # nothing to remediate


async def test_resolve_floor_margin_prefers_calibrated_noise_std(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # the noise floor is the ADVERSARY engine's run-to-run std; absent → the provisional margin.
    user_id = await _seed_user(owner_engine)
    async with owner_engine.begin() as conn:
        await upsert_calibration_bucket(
            conn,
            engine_version=ADVERSARY_VERSION,
            attribute_code="location",
            modality="text",
            signal="self_consistency",
            n=5,
            confidence_bucket=0.8,
            empirical_accuracy=0.7,
            ece=0.05,
            noise_std=0.04,
        )

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        calibrated = await resolve_floor_margin(conn, "location")
        provisional = await resolve_floor_margin(conn, "occupation")  # no bucket → provisional

    assert calibrated == pytest.approx(0.04)  # the calibrated adversary noise std
    assert provisional == pytest.approx(0.10)  # the provisional fallback margin
