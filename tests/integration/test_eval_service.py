"""Integration (M2.2): the eval service — seed → run engine → match → eval_results.

Real Alembic schema on a privileged connection (the eval is operator-run, like the seed). The
gateway is a fake returning fixed guesses per persona (no model), the embedder/geocoder are faked
(offline). Asserts one `eval` run, per-persona inferences persisted under it, per-attribute
eval_results (top-1/top-3 keyed to the fixed guesses), and RLS invisibility to normal users.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.core.config import get_database_settings
from app.db.rls import set_rls_context
from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.ingestion.sources.synthpai import parse_synthpai_rows
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.benchmark import seed_synthpai, synthpai_profile_id
from app.services.eval import run_eval
from app.services.geocoding import GeoResolution
from app.services.occupation import StringMatchJudge

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
        return None  # heuristic split kept; matching uses country/city names


class _FixedGateway:
    """Returns the same guesses every run (deterministic ensemble): location right, age wrong."""

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Lyon, France", self_confidence=0.9)],
            ),
            RawAttributeGuess(
                attribute="age",
                status="inferred",
                candidates=[RawCandidate(value_text="99", self_confidence=0.9)],
            ),
        ]


def _profile(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "age": 34,
        "sex": "female",
        "city_country": "Lyon, France",
        "birth_city_country": "Lyon, France",
        "education": "Masters in Chemistry",
        "occupation": "lab technician",
        "income": "40 thousand euros",
        "income_level": "middle",
        "relationship_status": "married",
    }
    return {**base, **overrides}


def _row(author: str, comment: str, profile: dict[str, Any]) -> dict[str, Any]:
    # every attribute revealed (certainty >= 1) so it counts toward the denominator
    reviews = {
        key: {"estimate": "", "hardness": 2, "certainty": 3}
        for key in ("city_country", "age", "sex", "occupation")
    }
    return {"author": author, "profile": profile, "text": comment, "reviews": {"human": reviews}}


_ROWS = [
    _row("pers1", "The funiculars here are great.", _profile()),
    _row("pers2", "Night shift again.", _profile(age=48, city_country="Lyon, France")),
]


@pytest.fixture(scope="module")
def eval_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(eval_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(eval_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(eval_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = eval_container.get_container_host_ip()
    port = eval_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed_and_eval(owner_engine: AsyncEngine, *, limit: int | None = None) -> Any:
    personas = parse_synthpai_rows(_ROWS)
    async with owner_engine.connect() as conn, conn.begin():
        await seed_synthpai(conn, _FakeEmbedder(), personas, master_key=_MASTER_KEY)
    async with owner_engine.connect() as conn, conn.begin():
        return await run_eval(
            conn,
            _FixedGateway(),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            master_key=_MASTER_KEY,
            judge=StringMatchJudge(),
            limit=limit,
            n_runs=2,
            temperature=0.0,
        )


async def test_eval_writes_one_run_and_per_attribute_results(owner_engine: AsyncEngine) -> None:
    result = await _seed_and_eval(owner_engine)

    assert result.personas == 2
    async with owner_engine.connect() as conn:
        run_type, run_status = (
            await conn.execute(
                text("SELECT type, status FROM runs WHERE id = :r"), {"r": result.run_id}
            )
        ).one()
        rows = (
            await conn.execute(
                text(
                    "SELECT attribute_code, top1_acc, top3_acc FROM eval_results "
                    "WHERE run_id = :r ORDER BY attribute_code"
                ),
                {"r": result.run_id},
            )
        ).all()
    assert run_type == "eval" and run_status == "succeeded"
    by_attr = {row[0]: (float(row[1]), float(row[2])) for row in rows}
    # only the 4 revealed attributes are scored; birthplace/education/income/relationship are
    # labeled but unrevealed (certainty 0) → excluded from the denominator entirely.
    assert set(by_attr) == {"location", "age", "occupation", "sex"}
    # location guessed "Lyon, France" for both personas → 100%; age guessed 99 → 0%.
    assert by_attr["location"] == (1.0, 1.0)
    assert by_attr["age"] == (0.0, 0.0)
    # occupation/sex were revealed but the engine never guessed them → scored as misses.
    assert by_attr["occupation"] == (0.0, 0.0)
    assert by_attr["sex"] == (0.0, 0.0)


async def test_eval_persists_per_persona_inferences_under_run(owner_engine: AsyncEngine) -> None:
    result = await _seed_and_eval(owner_engine)

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT profile_id, count(*) FROM inferences WHERE run_id = :r "
                    "GROUP BY profile_id"
                ),
                {"r": result.run_id},
            )
        ).all()
    by_profile = {row[0]: row[1] for row in rows}
    # inferences are tagged with each persona's own profile, not the run's session profile.
    assert by_profile[synthpai_profile_id("pers1")] == 2  # location + age
    assert by_profile[synthpai_profile_id("pers2")] == 2


async def _limited_persona(owner_engine: AsyncEngine) -> uuid.UUID:
    result = await _seed_and_eval(owner_engine, limit=1)
    async with owner_engine.connect() as conn:
        persona_ids = (
            await conn.execute(
                text("SELECT DISTINCT profile_id FROM inferences WHERE run_id = :r"),
                {"r": result.run_id},
            )
        ).all()
    assert len(persona_ids) == 1  # exactly one persona ran
    persona_id: uuid.UUID = persona_ids[0][0]
    return persona_id


async def test_limit_slices_the_same_persona_every_run(owner_engine: AsyncEngine) -> None:
    # the deterministic ORDER BY (created_at, id) must pick the same persona each --limit=1 run.
    first = await _limited_persona(owner_engine)
    second = await _limited_persona(owner_engine)

    assert first == second


async def test_eval_results_invisible_to_normal_users(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await _seed_and_eval(owner_engine)
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        # eval_results / eval_labels have no app-role grant → not selectable at all.
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute(text("SELECT count(*) FROM eval_results"))
