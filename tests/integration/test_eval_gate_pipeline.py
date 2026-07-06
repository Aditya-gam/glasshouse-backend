"""Integration (M2.6): the deterministic eval floor gate — the per-PR regression guard.

Runs the eval pipeline (retrieve → normalize → cluster → match → score) over a fixed fixture with a
replay gateway (no model), then checks the per-attribute top-1 against a fixture floor. A regression
in the deterministic engine (normalizer, match rules, clustering, calibration bucketing) shifts the
fixture accuracy and trips the floor → the build fails. The model/prompt gate needs a real benchmark
(`scripts.eval_gate` after `scripts.run_eval`) — that runs on the accuracy numbers, not fixtures.
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.core.config import get_database_settings
from app.domain.eval_gate import check_eval_floor
from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.ingestion.sources.synthpai import parse_synthpai_rows
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.eval import run_eval
from app.services.geocoding import GeoResolution
from app.services.occupation import StringMatchJudge

_MASTER_KEY = "test-master-key-not-a-real-secret"

# The fixture floor: the clean deterministic pipeline gets these five right (top-1 = 1.0). Age is
# deliberately guessed wrong, so it is NOT floored here (see the regression test).
_FIXTURE_FLOOR = {
    "location": 1.0,
    "income": 1.0,
    "education": 1.0,
    "sex": 1.0,
    "relationship": 1.0,
}


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


class _GateGateway:
    """Replays fixed guesses that exercise the normalizer + match rules; age is wrong on purpose."""

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        def guess(attribute: str, value: str) -> RawAttributeGuess:
            return RawAttributeGuess(
                attribute=attribute,  # type: ignore[arg-type]
                status="inferred",
                candidates=[RawCandidate(value_text=value, self_confidence=0.9)],
            )

        return [
            guess("location", "Lyon, France"),  # geo name-set → correct
            guess("income", "45000"),  # → bracket medium == label "middle"
            guess("education", "Masters in Chemistry"),  # → master
            guess("sex", "female"),
            guess("relationship", "married"),
            guess("age", "99"),  # deliberately wrong (label 34)
        ]


def _profile() -> dict[str, Any]:
    return {
        "age": 34,
        "sex": "female",
        "city_country": "Lyon, France",
        "birth_city_country": "Lyon, France",
        "education": "Masters in Chemistry",
        "occupation": "lab technician",
        "income": "45 thousand euros",
        "income_level": "middle",
        "relationship_status": "married",
    }


_REVIEWED = {"hardness": 2, "certainty": 3, "estimate": ""}
_ROWS = [
    {
        "author": "pers1",
        "profile": _profile(),
        "text": "The funiculars here are great.",
        "reviews": {
            "human": {
                key: _REVIEWED
                for key in (
                    "city_country",
                    "age",
                    "sex",
                    "education",
                    "income_level",
                    "relationship_status",
                )
            }
        },
    }
]


@pytest.fixture(scope="module")
def gate_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(gate_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(gate_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


async def _run_gate(owner_engine: AsyncEngine) -> dict[str, float]:
    from app.services.benchmark import seed_synthpai

    async with owner_engine.connect() as conn, conn.begin():
        await seed_synthpai(
            conn, _FakeEmbedder(), parse_synthpai_rows(_ROWS), master_key=_MASTER_KEY
        )
    async with owner_engine.connect() as conn, conn.begin():
        result = await run_eval(
            conn,
            _GateGateway(),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            master_key=_MASTER_KEY,
            judge=StringMatchJudge(),
            n_runs=2,
            temperature=0.0,
        )
    return {score.attribute: score.top1_acc for score in result.scores}


async def test_gate_passes_on_the_clean_pipeline(owner_engine: AsyncEngine) -> None:
    top1 = await _run_gate(owner_engine)

    assert check_eval_floor(top1, _FIXTURE_FLOOR) == []  # clean engine → no breach → build passes


async def test_gate_fires_when_an_attribute_regresses(owner_engine: AsyncEngine) -> None:
    top1 = await _run_gate(owner_engine)

    # age was guessed wrong (top-1 = 0); a floor that expects it caught the regression.
    breaches = check_eval_floor(top1, {**_FIXTURE_FLOOR, "age": 0.5})

    assert [b.attribute for b in breaches] == ["age"]
    assert breaches[0].top1_acc == 0.0
