"""Integration (M1.3): ingest → persist stores items encrypted + embedded + deduped.

Runs against the real v2 schema (Alembic migrations, not the tracer schema), as the non-superuser
app role with the user's RLS context. The embedder is faked (no model download in CI); the rule-5
drop is exercised end-to-end (a third-party record never reaches `items`).
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
from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.retrieval.embedder import EMBEDDING_DIM
from app.services.ingestion import PersistResult, ingest_and_persist

_MASTER_KEY = "test-master-key-not-a-real-secret"


class _FakeEmbedder:
    """Deterministic, network-free embedder — `EMBEDDING_DIM` floats per text, no model download."""

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float((len(t) + i) % 7) for i in range(EMBEDDING_DIM)] for t in texts]


class _FakeAdapter:
    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


@pytest.fixture(scope="module")
def persist_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(persist_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(persist_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(persist_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = persist_container.get_container_host_ip()
    port = persist_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed_user(owner_engine: AsyncEngine) -> uuid.UUID:
    """Create a user + provision their DEK (privileged, owner connection)."""
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    return user_id


async def _persist(
    app_engine: AsyncEngine, user_id: uuid.UUID, records: list[ParsedTextRecord]
) -> PersistResult:
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        return await ingest_and_persist(
            conn,
            _FakeEmbedder(),
            _FakeAdapter(records),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )


async def test_persist_stores_encrypted_embedded_and_drops_third_party(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    records = [
        ParsedTextRecord(
            text="I hike near Gas Works Park in Seattle on PST mornings.", is_subject_authored=True
        ),
        ParsedTextRecord(
            text="A third party wrote this English sentence and must be dropped.",
            is_subject_authored=False,
        ),
    ]

    result = await _persist(app_engine, user_id, records)

    assert result.inserted == 1  # the third-party record was dropped before persist
    assert result.deduped == 0

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT text_ct, decrypt_field(owner_user_id, text_ct, :mk), "
                    "vector_dims(embedding), content_hmac, is_subject_authored "
                    "FROM items WHERE owner_user_id = :uid"
                ),
                {"mk": _MASTER_KEY, "uid": user_id},
            )
        ).all()

    assert len(rows) == 1
    text_ct, decrypted, dims, content_hmac, authored = rows[0]
    assert decrypted == "I hike near Gas Works Park in Seattle on PST mornings."
    assert bytes(text_ct) != decrypted.encode()  # stored ciphertext, never plaintext
    assert dims == EMBEDDING_DIM  # embedded at the configured dimension
    assert content_hmac and authored is True


async def test_reingest_same_content_is_deduped(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    records = [ParsedTextRecord(text="My only post, in English.", is_subject_authored=True)]

    first = await _persist(app_engine, user_id, records)
    second = await _persist(app_engine, user_id, records)

    assert first.inserted == 1
    assert second.inserted == 0 and second.deduped == 1  # same keyed HMAC → ON CONFLICT skip
    async with owner_engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM items WHERE owner_user_id = :uid"), {"uid": user_id}
            )
        ).scalar_one()
    assert count == 1
