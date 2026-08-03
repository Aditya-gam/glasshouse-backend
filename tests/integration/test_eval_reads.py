"""Integration (M2): GET /v1/eval/results + /v1/eval/calibration — the public trust reads.

Real Alembic schema. eval_results/calibration are seeded on a privileged (owner) connection, then
read back through the **public** app-role endpoints (no auth, no RLS context). Asserts the newest
eval run wins, the calibration curve pools buckets, empty is empty, and the `latest_eval_run_id()`
SECURITY DEFINER helper — not `runs` RLS — is what lets the no-context reader find the run.
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
from app.api.v1 import evals
from app.core.config import get_database_settings
from app.gateway.prompts import ENGINE_VERSION
from app.main import app
from app.repositories.calibration import upsert_calibration_bucket
from app.repositories.eval_results import insert_eval_result, latest_eval_results
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2

# (attribute_code, top1_acc, top3_acc) and (attribute_code, confidence_bucket, empirical_accuracy).
_RESULTS = [("location", 1.0, 1.0), ("age", 0.0, 0.0)]
_CALIB = [("location", 0.9, 1.0), ("age", 0.9, 0.0), ("age", 0.5, 0.4)]


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


@pytest_asyncio.fixture(autouse=True)
async def _clean(owner_engine: AsyncEngine) -> None:
    """Each test owns "the latest eval run", so clear prior rows first (the DB is module-shared)."""
    async with owner_engine.connect() as conn, conn.begin():
        await conn.execute(text("DELETE FROM eval_results"))
        await conn.execute(text("DELETE FROM calibration"))
        await conn.execute(text("DELETE FROM runs WHERE type = 'eval'"))


@pytest_asyncio.fixture
async def client(
    app_engine: AsyncEngine,
) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_eval_run(
    owner_engine: AsyncEngine,
    *,
    results: list[tuple[str, float, float]],
    calib: list[tuple[str, float, float]],
    engine_version: str = ENGINE_VERSION,
    results_engine_version: str | None = None,
    age_seconds: int = 0,
) -> uuid.UUID:
    """Seed one eval run (owner conn bypasses RLS) `age_seconds` in the past; return its run id.

    `engine_version` keys the run + calibration (the bare attack engine); `results_engine_version`
    (default = `engine_version`) is `eval_results`' — a judge-graded run stores the suffixed scoring
    version there while the calibration map stays bare, and the two must be allowed to diverge.
    """
    eval_results_version = results_engine_version or engine_version
    async with owner_engine.connect() as conn, conn.begin():
        user_id = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="eval", status="succeeded", engine_version=engine_version
        )
        if age_seconds:
            await conn.execute(
                text(
                    "UPDATE runs SET created_at = now() - (:s * interval '1 second') WHERE id = :r"
                ),
                {"s": age_seconds, "r": run_id},
            )
        for attr, top1, top3 in results:
            await insert_eval_result(
                conn,
                run_id=run_id,
                attribute_code=attr,
                modality="text",
                top1_acc=top1,
                top3_acc=top3,
                by_hardness={},
                engine_version=eval_results_version,
            )
        for attr, bucket, empirical in calib:
            await upsert_calibration_bucket(
                conn,
                engine_version=engine_version,
                attribute_code=attr,
                modality="text",
                signal="self_consistency",
                n=2,
                confidence_bucket=bucket,
                empirical_accuracy=empirical,
                ece=0.0,
            )
    return run_id


async def test_results_empty_before_any_eval_run(client: AsyncClient) -> None:
    response = await client.get("/v1/eval/results")

    assert response.status_code == 200
    assert response.json() == []


async def test_calibration_empty_before_any_eval_run(client: AsyncClient) -> None:
    response = await client.get("/v1/eval/calibration")

    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["calibration"] == []


async def test_results_returns_latest_run_per_attribute(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    await _seed_eval_run(owner_engine, results=_RESULTS, calib=_CALIB)

    response = await client.get("/v1/eval/results")

    assert response.status_code == 200
    by_attr = {row["attribute"]: row for row in response.json()}
    assert set(by_attr) == {"location", "age"}
    assert by_attr["location"]["top1"] == 1.0
    assert by_attr["location"]["top3"] == 1.0
    assert by_attr["location"]["modality"] == "text"
    assert by_attr["location"]["engine_version"] == ENGINE_VERSION
    assert by_attr["age"]["top1"] == 0.0


async def test_calibration_pools_buckets_and_orders_ascending(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    await _seed_eval_run(owner_engine, results=_RESULTS, calib=_CALIB)

    response = await client.get("/v1/eval/calibration")

    assert response.status_code == 200
    body = response.json()
    # rows mirror the per-attribute accuracy (label = attribute code)
    by_label = {row["label"]: (row["top1"], row["top3"]) for row in body["rows"]}
    assert by_label == {"location": (1.0, 1.0), "age": (0.0, 0.0)}
    # the 0.9 bucket pools location (1.0) + age (0.0) → 0.5; ascending by predicted bucket.
    assert body["calibration"] == [[0.5, 0.4], [0.9, 0.5]]


async def test_calibration_curve_keys_on_the_bare_engine_not_the_scoring_version(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    # a judge-graded run: eval_results carries the +match_judge scoring suffix (accuracy provenance)
    # while runs + calibration stay keyed on the bare engine. The curve must follow the bare engine
    # (runs.engine_version), not eval_results' suffixed version — else the public curve goes blank.
    scoring_version = f"{ENGINE_VERSION}+match_judge_v1@judge"
    await _seed_eval_run(
        owner_engine, results=_RESULTS, calib=_CALIB, results_engine_version=scoring_version
    )

    calibration = await client.get("/v1/eval/calibration")
    results = await client.get("/v1/eval/results")

    # the curve is the bare-keyed map (non-empty), not an empty lookup on the suffixed version.
    assert calibration.json()["calibration"] == [[0.5, 0.4], [0.9, 0.5]]
    # ...and /results faithfully surfaces the scoring provenance (the suffixed version).
    assert all(row["engine_version"] == scoring_version for row in results.json())


async def test_only_the_newest_eval_run_is_served(
    client: AsyncClient, owner_engine: AsyncEngine
) -> None:
    # a stale run (an hour old) with different numbers must be shadowed by the fresh run.
    await _seed_eval_run(
        owner_engine,
        results=[("location", 0.1, 0.2)],
        calib=[("location", 0.9, 0.1)],
        age_seconds=3600,
    )
    await _seed_eval_run(owner_engine, results=_RESULTS, calib=_CALIB)

    response = await client.get("/v1/eval/results")

    by_attr = {row["attribute"]: row["top1"] for row in response.json()}
    assert by_attr == {"location": 1.0, "age": 0.0}  # the fresh run, not the stale 0.1


async def test_public_reader_needs_the_security_definer_helper_not_runs_rls(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # the eval run belongs to a session profile the public reader doesn't own, so runs RLS hides
    # it; only latest_eval_run_id() (SECURITY DEFINER) resolves it — that is what makes reads work.
    await _seed_eval_run(owner_engine, results=_RESULTS, calib=_CALIB)

    async with app_engine.connect() as conn:
        # app-role SELECT on eval_results is granted (0010)...
        eval_count = (await conn.execute(text("SELECT count(*) FROM eval_results"))).scalar_one()
        assert eval_count == 2
        # ...but the eval run itself is invisible to a no-context reader through runs RLS...
        visible_runs = (
            await conn.execute(text("SELECT count(*) FROM runs WHERE type = 'eval'"))
        ).scalar_one()
        assert visible_runs == 0
        # ...yet the helper still resolves it, so the read returns rows.
        assert (await conn.execute(text("SELECT latest_eval_run_id()"))).scalar_one() is not None
        rows = await latest_eval_results(conn)
        assert len(rows) == 2


async def test_read_handlers_directly_for_coverage(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # the endpoint bodies run under httpx-ASGI (untraced by CI coverage); call them directly too.
    await _seed_eval_run(owner_engine, results=_RESULTS, calib=_CALIB)

    async with app_engine.connect() as conn:
        results = await evals.eval_results(conn)
        benchmark = await evals.eval_calibration(conn)

    assert {row.attribute for row in results} == {"location", "age"}
    assert benchmark.calibration == [(0.5, 0.4), (0.9, 0.5)]
    assert {row.label for row in benchmark.rows} == {"location", "age"}
