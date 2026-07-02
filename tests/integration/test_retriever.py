"""Integration (M1.6): the Retriever selects recall-first under budget; always-include survives.

Real v2 schema (migrations), app-role + RLS. Items are seeded via the M1.3 persist path so they
carry embeddings; the embedder, PII detector, and token counter are faked (no model downloads).
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
from app.repositories import import_sources as import_sources_repo
from app.repositories import items as items_repo
from app.repositories.items import RetrievedItem
from app.repositories.profiles import get_or_create_self_profile
from app.retrieval.embedder import EMBEDDING_DIM
from app.retrieval.retriever import RetrievalConfig, retrieve_evidence
from app.services.ingestion import ingest_and_persist

_MASTER_KEY = "test-master-key-not-a-real-secret"


class _FakeEmbedder:
    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float((len(t) + i) % 7) for i in range(EMBEDDING_DIM)] for t in texts]


class _FakeDetector:
    """Flags the always-include marker — deterministic, no spaCy model."""

    def has_identifying_signal(self, text: str) -> bool:
        return "FLAGME" in text


class _FakeAdapter:
    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


def _fixed_tokens(_text: str) -> int:
    return 10  # every item costs 10 tokens → deterministic budget arithmetic


@pytest.fixture(scope="module")
def retriever_container() -> Iterator[PostgresContainer]:
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
async def owner_engine(retriever_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(retriever_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(retriever_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = retriever_container.get_container_host_ip()
    port = retriever_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _seed_user_with_items(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, texts: list[str]
) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    records = [ParsedTextRecord(text=t, is_subject_authored=True) for t in texts]
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        await ingest_and_persist(
            conn,
            _FakeEmbedder(),
            _FakeAdapter(records),
            owner_user_id=user_id,
            master_key=_MASTER_KEY,
        )
    return user_id


async def _retrieve(
    app_engine: AsyncEngine, user_id: uuid.UUID, budget: int, profile_id: uuid.UUID | None = None
) -> list[RetrievedItem]:
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        if profile_id is None:
            profile_id = await get_or_create_self_profile(conn, user_id)
        return await retrieve_evidence(
            conn,
            _FakeEmbedder(),
            _FakeDetector(),
            profile_id=profile_id,
            master_key=_MASTER_KEY,
            config=RetrievalConfig(token_budget=budget),
            token_counter=_fixed_tokens,
        )


async def test_small_footprint_passes_through_whole(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    texts = ["I walk to Gas Works Park.", "Random coffee thought.", "Standup is at nine."]
    user_id = await _seed_user_with_items(owner_engine, app_engine, texts)

    evidence = await _retrieve(app_engine, user_id, budget=1000)

    assert {e.text for e in evidence} == set(texts)  # generous budget → everything


async def test_always_include_survives_tiny_budget(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    texts = [
        "A neutral morning note.",
        "FLAGME I work at Acme in Seattle.",
        "Another neutral note.",
    ]
    user_id = await _seed_user_with_items(owner_engine, app_engine, texts)

    # budget=5, every item=10 tokens → no non-mandatory item fits; the FLAGME item is never dropped.
    evidence = await _retrieve(app_engine, user_id, budget=5)

    assert [e.text for e in evidence] == ["FLAGME I work at Acme in Seattle."]


async def _add_profile_with_item(
    app_engine: AsyncEngine, user_id: uuid.UUID, item_text: str
) -> uuid.UUID:
    """A second (benchmark-style) profile under the same owner, holding one item."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id: uuid.UUID = (
            await conn.execute(
                text("INSERT INTO profiles (type, user_id) VALUES ('synthpai', :u) RETURNING id"),
                {"u": user_id},
            )
        ).scalar_one()
        source_id = await import_sources_repo.create_import_source(
            conn, profile_id, platform="synthpai", method="loader"
        )
        await items_repo.insert_canonical_item(
            conn,
            profile_id=profile_id,
            owner_user_id=user_id,
            import_source_id=source_id,
            plaintext=item_text,
            embedding=_FakeEmbedder().embed([item_text])[0],
            posted_at=None,
            original_tz=None,
            is_subject_authored=True,
            master_key=_MASTER_KEY,
        )
    return profile_id


async def test_retrieval_never_blends_profiles(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """One owner, two profiles: each profile's evidence set holds only its own items."""
    self_texts = ["I walk to Gas Works Park.", "Standup is at nine."]
    user_id = await _seed_user_with_items(owner_engine, app_engine, self_texts)
    other_profile = await _add_profile_with_item(app_engine, user_id, "I bike along the Seine.")

    self_evidence = await _retrieve(app_engine, user_id, budget=1000)
    other_evidence = await _retrieve(app_engine, user_id, budget=1000, profile_id=other_profile)

    assert {e.text for e in self_evidence} == set(self_texts)
    assert [e.text for e in other_evidence] == ["I bike along the Seine."]


async def test_same_text_in_two_profiles_is_two_items(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Content dedupe is per profile: identical text under two personas must not be dropped."""
    duplicate = "Same here."
    user_id = await _seed_user_with_items(owner_engine, app_engine, [duplicate])
    other_profile = await _add_profile_with_item(app_engine, user_id, duplicate)

    other_evidence = await _retrieve(app_engine, user_id, budget=1000, profile_id=other_profile)

    assert [e.text for e in other_evidence] == [duplicate]  # not deduped away cross-profile
