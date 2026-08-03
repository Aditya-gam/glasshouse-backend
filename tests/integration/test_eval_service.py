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
from app.gateway.client import MatchJudgeResult, MatchVerdictLabel
from app.gateway.prompts import ENGINE_VERSION
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


class _OccupationGateway:
    """Predicts an occupation paraphrase of the label ("lab technician") — a deterministic miss."""

    async def profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="occupation",
                status="inferred",
                candidates=[RawCandidate(value_text="laboratory technician", self_confidence=0.9)],
            )
        ]


class _FakeMatchJudge:
    """A judge that returns a fixed verdict for every ambiguous case (records the calls)."""

    def __init__(self, verdict: MatchVerdictLabel = "yes", confidence: float = 0.95) -> None:
        self._verdict = verdict
        self._confidence = confidence
        self.calls = 0

    async def judge(self, attribute: Any, prediction: str, ground_truth: str) -> MatchJudgeResult:
        self.calls += 1
        return MatchJudgeResult(reasoning="", verdict=self._verdict, confidence=self._confidence)


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


async def _seed_and_eval(
    owner_engine: AsyncEngine,
    *,
    limit: int | None = None,
    gateway: Any = None,
    match_judge: Any = None,
) -> Any:
    personas = parse_synthpai_rows(_ROWS)
    async with owner_engine.connect() as conn, conn.begin():
        await seed_synthpai(conn, _FakeEmbedder(), personas, master_key=_MASTER_KEY)
    async with owner_engine.connect() as conn, conn.begin():
        return await run_eval(
            conn,
            gateway or _FixedGateway(),
            _FakeEmbedder(),
            _FakeDetector(),
            _FakeGeocoder(),
            master_key=_MASTER_KEY,
            judge=StringMatchJudge(),
            match_judge=match_judge,
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
                    "SELECT attribute_code, top1_acc, top3_acc, engine_version FROM eval_results "
                    "WHERE run_id = :r ORDER BY attribute_code"
                ),
                {"r": result.run_id},
            )
        ).all()
    assert run_type == "eval" and run_status == "succeeded"
    # no match-judge → the scoring version pins the bare attack engine, no judge suffix
    # (the calibration coupling: only a judge-graded run carries "+match_judge_v1@judge").
    assert all(row[3] == ENGINE_VERSION for row in rows)
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


async def test_eval_writes_calibration_map_pinned_to_the_attack_engine(
    owner_engine: AsyncEngine,
) -> None:
    # both personas predict location "Lyon, France" correctly at raw 1.0 (2-run agreement) → the
    # [0.9,1.0] location bucket calibrates to empirical 1.0; age (99) is wrong → 0.0. The map is
    # keyed by the bare attack ENGINE_VERSION so a user's inference can find it.
    await _seed_and_eval(owner_engine)

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT attribute_code, confidence_bucket, empirical_accuracy, signal, n, "
                    "modality, engine_version, noise_std FROM calibration "
                    "WHERE engine_version = :ev ORDER BY attribute_code, confidence_bucket"
                ),
                {"ev": ENGINE_VERSION},  # no judge → pinned to the bare attack engine
            )
        ).all()
    by_attr = {row[0]: row for row in rows}
    assert "location" in by_attr and "age" in by_attr
    location = by_attr["location"]
    assert float(location[1]) == 0.9 and float(location[2]) == 1.0  # bucket [0.9,1.0] → 1.0
    assert location[3] == "self_consistency" and location[4] == 2  # signal + N
    assert location[5] == "text" and location[6] == ENGINE_VERSION
    assert location[7] is None  # noise_std deferred (M2.4 ships map + ECE only)
    assert float(by_attr["age"][2]) == 0.0  # wrong age guess calibrates to 0


async def test_reseeding_upserts_calibration_in_place(owner_engine: AsyncEngine) -> None:
    await _seed_and_eval(owner_engine)
    await _seed_and_eval(owner_engine)  # a second pass at the same engine version

    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM calibration "
                    "WHERE engine_version = :ev AND attribute_code = 'location'"
                ),
                {"ev": ENGINE_VERSION},
            )
        ).scalar_one()
    assert count == 1  # one bucket per (engine, attr, modality, signal, n) — upserted, not doubled


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


async def test_eval_ground_truth_invisible_to_normal_users(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # eval_results holds non-personal benchmark accuracy — public (app-role SELECT granted in 0010
    # for the trust reads). The benchmark ground-truth `eval_labels` stays grant-locked, so a normal
    # user can read the numbers but never the answer key.
    await _seed_and_eval(owner_engine)
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        eval_count = (await conn.execute(text("SELECT count(*) FROM eval_results"))).scalar_one()
        assert eval_count > 0  # the public benchmark numbers are readable...
        with pytest.raises(Exception, match="permission denied"):  # ...the answer key is not.
            await conn.execute(text("SELECT count(*) FROM eval_labels"))


async def test_match_judge_upgrades_occupation_and_flags_spot_checks(
    owner_engine: AsyncEngine,
) -> None:
    # occupation "laboratory technician" is a deterministic string-miss vs the label "lab
    # technician"; the judge (yes, low confidence) upgrades it to a hit and flags a spot-check.
    judge = _FakeMatchJudge(verdict="yes", confidence=0.5)

    result = await _seed_and_eval(owner_engine, gateway=_OccupationGateway(), match_judge=judge)

    async with owner_engine.connect() as conn:
        top1_acc, results_version = (
            await conn.execute(
                text(
                    "SELECT top1_acc, engine_version FROM eval_results "
                    "WHERE run_id = :r AND attribute_code = 'occupation'"
                ),
                {"r": result.run_id},
            )
        ).one()
        calibration_versions = [
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT DISTINCT engine_version FROM calibration "
                    "WHERE attribute_code = 'occupation'"
                )
            )
        ]
    assert float(top1_acc) == 1.0  # both personas upgraded from string-miss to judged hit
    # eval_results pins the judge (the accuracy provenance)...
    assert results_version.endswith("+match_judge_v1@judge")
    # ...but the calibration map keys to the BARE attack engine, so a user's inference finds it.
    assert calibration_versions == [ENGINE_VERSION]
    assert judge.calls == 2  # one ambiguous occupation per persona
    # low-confidence "yes" → both raised a spot-check.
    assert len(result.spot_checks) == 2
    assert all(s.attribute == "occupation" for s in result.spot_checks)
