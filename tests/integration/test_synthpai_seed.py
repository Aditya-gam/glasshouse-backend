"""Integration (M2.1): the SynthPAI seed — benchmark user + profiles + encrypted items + labels.

Real Alembic schema on a privileged connection (the seed is operator-run, like a migration);
the embedder is faked (no model downloads, no network). Asserts idempotent re-seeding, encrypted
storage, per-persona profile mapping, and that benchmark rows stay RLS-invisible to normal users.
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
from app.ingestion.sources.synthpai import parse_synthpai_rows
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.benchmark import SYNTHPAI_USER_ID, seed_synthpai, synthpai_profile_id

_MASTER_KEY = "test-master-key-not-a-real-secret"


class _FakeEmbedder:
    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float((len(t) + i) % 7) for i in range(EMBEDDING_DIM)] for t in texts]


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
    reviews = {"city_country": {"estimate": "", "hardness": 2, "certainty": 3}}
    return {
        "author": author,
        "profile": profile,
        "text": comment,
        "reviews": {"human": reviews},
    }


_ROWS = [
    _row("pers1", "The funiculars here beat any commute.", _profile()),
    _row("pers1", "Weekend market on the Saône again.", _profile()),
    _row("pers2", "Night shifts at the plant are brutal.", _profile(age=48, sex="male")),
]


@pytest.fixture(scope="module")
def seed_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(seed_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(seed_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(seed_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = seed_container.get_container_host_ip()
    port = seed_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed(owner_engine: AsyncEngine, rows: list[dict[str, Any]] | None = None) -> None:
    personas = parse_synthpai_rows(rows if rows is not None else _ROWS)
    async with owner_engine.connect() as conn, conn.begin():
        await seed_synthpai(conn, _FakeEmbedder(), personas, master_key=_MASTER_KEY)


async def test_seed_creates_profiles_items_and_labels(owner_engine: AsyncEngine) -> None:
    await _seed(owner_engine)

    async with owner_engine.connect() as conn:
        profiles = (
            await conn.execute(
                text("SELECT count(*) FROM profiles WHERE type = 'synthpai' AND user_id = :u"),
                {"u": SYNTHPAI_USER_ID},
            )
        ).scalar_one()
        items = (
            await conn.execute(
                text("SELECT count(*) FROM items WHERE owner_user_id = :u"),
                {"u": SYNTHPAI_USER_ID},
            )
        ).scalar_one()
        labels = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM eval_labels WHERE profile_id IN "
                    "(SELECT id FROM profiles WHERE type = 'synthpai')"
                )
            )
        ).scalar_one()
    assert profiles == 2 and items == 3 and labels == 16  # 2 personas × 8 attributes


async def test_items_are_encrypted_and_round_trip(owner_engine: AsyncEngine) -> None:
    await _seed(owner_engine)

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT text_ct, decrypt_field(owner_user_id, text_ct, :mk) FROM items "
                    "WHERE profile_id = :p"
                ),
                {"mk": _MASTER_KEY, "p": synthpai_profile_id("pers2")},
            )
        ).all()
    assert len(rows) == 1
    ciphertext, plaintext = rows[0]
    assert plaintext == "Night shifts at the plant are brutal."
    assert plaintext.encode() not in bytes(ciphertext)  # stored encrypted, not plaintext bytes


async def test_labels_map_and_carry_review_aggregates(owner_engine: AsyncEngine) -> None:
    await _seed(owner_engine)

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT attribute_code, true_value FROM eval_labels WHERE profile_id = :p"),
                {"p": synthpai_profile_id("pers1")},
            )
        ).all()
    by_code = {row[0]: row[1] for row in rows}
    assert by_code["location"] == {"value": "Lyon, France", "hardness": 2, "certainty": 3}
    assert by_code["age"]["value"] == 34
    assert by_code["sex"] == {"value": "female", "hardness": None, "certainty": 0}
    assert by_code["income"]["value"] == "middle"


async def test_reseeding_is_idempotent(owner_engine: AsyncEngine) -> None:
    await _seed(owner_engine)
    await _seed(owner_engine)  # second pass: items dedupe, labels upsert, ids stable

    async with owner_engine.connect() as conn:
        items = (
            await conn.execute(
                text("SELECT count(*) FROM items WHERE owner_user_id = :u"),
                {"u": SYNTHPAI_USER_ID},
            )
        ).scalar_one()
        labels = (await conn.execute(text("SELECT count(*) FROM eval_labels"))).scalar_one()
        deks = (
            await conn.execute(
                text("SELECT count(*) FROM data_keys WHERE user_id = :u"),
                {"u": SYNTHPAI_USER_ID},
            )
        ).scalar_one()
        sources = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM import_sources WHERE profile_id IN "
                    "(SELECT id FROM profiles WHERE type = 'synthpai')"
                )
            )
        ).scalar_one()
    # one loader source per persona — re-seeding must not grow provenance rows either.
    assert items == 3 and labels == 16 and deks == 1 and sources == 2


async def test_reseeding_with_changed_labels_updates_in_place(owner_engine: AsyncEngine) -> None:
    """The upsert's UPDATE path: a corrected label overwrites the stored value, no new row."""
    await _seed(owner_engine)
    nice = _profile(city_country="Nice, France")
    corrected = [_row("pers1", "The funiculars here beat any commute.", nice)]

    await _seed(owner_engine, rows=corrected)

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT true_value FROM eval_labels "
                    "WHERE profile_id = :p AND attribute_code = 'location'"
                ),
                {"p": synthpai_profile_id("pers1")},
            )
        ).all()
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0][0]["value"] == "Nice, France"


async def test_benchmark_rows_invisible_to_normal_users(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await _seed(owner_engine)
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        visible_items = (await conn.execute(text("SELECT count(*) FROM items"))).scalar_one()
        visible_profiles = (
            await conn.execute(text("SELECT count(*) FROM profiles WHERE type = 'synthpai'"))
        ).scalar_one()

    assert visible_items == 0 and visible_profiles == 0  # RLS: benchmark data is not shared
