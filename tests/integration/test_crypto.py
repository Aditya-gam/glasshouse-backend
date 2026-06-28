"""M0.8 crypto gate on the v2 *migrated* schema — round-trip, crypto-shred, data_keys lockout.

The SECURITY DEFINER crypto fns + the app-role lockout were first proven in T2 against the tracer
schema; here they run on the production 0001 migration. Plus the master-key (KMS-unwrap) seam.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

import app.db.crypto as crypto_mod
from alembic import command
from app.core.config import CryptoSettings, get_database_settings

_MASTER_KEY = "test-master-key-not-real"


def test_get_master_key_returns_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_mod, "get_crypto_settings", lambda: CryptoSettings(master_key="k"))
    assert crypto_mod.get_master_key() == "k"


def test_get_master_key_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_mod, "get_crypto_settings", lambda: CryptoSettings(master_key=None))
    with pytest.raises(crypto_mod.MasterKeyUnavailableError):
        crypto_mod.get_master_key()


@pytest.fixture(scope="module")
def crypto_container() -> Iterator[tuple[PostgresContainer, uuid.UUID, uuid.UUID]]:
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
        with psycopg.connect(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user="glasshouse",
            password="glasshouse",
            dbname="glasshouse",
            autocommit=True,
        ) as conn:

            def provisioned(clerk_id: str) -> uuid.UUID:
                row = conn.execute(
                    "INSERT INTO users (clerk_user_id) VALUES (%s) RETURNING id", (clerk_id,)
                ).fetchone()
                assert row is not None
                user_id: uuid.UUID = row[0]
                conn.execute("SELECT provision_user_dek(%s, %s)", (user_id, _MASTER_KEY))
                return user_id

            yield container, provisioned("rt"), provisioned("shred")


@pytest_asyncio.fixture
async def owner_engine(
    crypto_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID],
) -> AsyncIterator[AsyncEngine]:
    container, _, _ = crypto_container
    engine = create_async_engine(container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(
    crypto_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID],
) -> AsyncIterator[AsyncEngine]:
    container, _, _ = crypto_container
    host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
    engine = create_async_engine(
        f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    )
    yield engine
    await engine.dispose()


async def test_round_trip(
    owner_engine: AsyncEngine, crypto_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID]
) -> None:
    _, user_rt, _ = crypto_container
    async with owner_engine.begin() as conn:
        ciphertext = (
            await conn.execute(
                text("SELECT encrypt_field(:u, :p, :k)"),
                {"u": user_rt, "p": "I live in Lisbon", "k": _MASTER_KEY},
            )
        ).scalar_one()
        plaintext = (
            await conn.execute(
                text("SELECT decrypt_field(:u, :ct, :k)"),
                {"u": user_rt, "ct": ciphertext, "k": _MASTER_KEY},
            )
        ).scalar_one()
    assert plaintext == "I live in Lisbon"


async def test_crypto_shred(
    owner_engine: AsyncEngine, crypto_container: tuple[PostgresContainer, uuid.UUID, uuid.UUID]
) -> None:
    _, _, user_shred = crypto_container
    async with owner_engine.begin() as conn:
        ciphertext = (
            await conn.execute(
                text("SELECT encrypt_field(:u, :p, :k)"),
                {"u": user_shred, "p": "secret", "k": _MASTER_KEY},
            )
        ).scalar_one()
        await conn.execute(text("DELETE FROM data_keys WHERE user_id = :u"), {"u": user_shred})
    async with owner_engine.begin() as conn:
        with pytest.raises(DBAPIError):
            await conn.execute(
                text("SELECT decrypt_field(:u, :ct, :k)"),
                {"u": user_shred, "ct": ciphertext, "k": _MASTER_KEY},
            )


async def test_app_role_cannot_read_data_keys(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn, conn.begin():
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT wrapped_dek FROM data_keys"))
