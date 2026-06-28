"""M0.5 mandatory gate — RLS tenant isolation across every owned table.

Applies 0001+0002 to a fresh database, seeds two users (A, B) with full FK chains as the owner,
then, as the non-superuser app role, asserts: each user sees only their own rows (read), can't
write into another tenant's scope (WITH CHECK), an unscoped session sees nothing (fail-closed),
and the app role cannot read `data_keys`.
"""

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

from alembic import command
from app.core.config import get_database_settings
from app.db.rls import set_rls_context

_USERS = {"A": uuid.uuid4(), "B": uuid.uuid4()}

# Every owned table + the column to read for the visibility check (all have an `id` except none).
_OWNED_TABLES = (
    "profiles",
    "items",
    "media_assets",
    "connected_accounts",
    "consents",
    "import_sources",
    "runs",
    "inferences",
    "inference_candidates",
    "inference_evidence",
    "run_metrics",
    "exif_findings",
    "remediations",
)


def _seed_chain(cur: psycopg.Cursor, user_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Insert a full owned-data chain for `user_id` (as the owner, bypassing RLS)."""

    def one(sql: str, *params: object) -> uuid.UUID:
        row = cur.execute(sql, params).fetchone()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]

    cur.execute("INSERT INTO users (id) VALUES (%s)", (user_id,))
    pid = one("INSERT INTO profiles (type, user_id) VALUES ('self', %s) RETURNING id", user_id)
    isrc = one(
        "INSERT INTO import_sources (profile_id, platform, method) "
        "VALUES (%s, 'reddit', 'upload') RETURNING id",
        pid,
    )
    item = one(
        "INSERT INTO items (profile_id, owner_user_id, text_ct, content_hmac) "
        "VALUES (%s, %s, %s, 'h') RETURNING id",
        pid,
        user_id,
        b"\x00",
    )
    media = one(
        "INSERT INTO media_assets (profile_id, owner_user_id, object_ref, content_hmac, mime) "
        "VALUES (%s, %s, 'o', 'h', 'image/jpeg') RETURNING id",
        pid,
        user_id,
    )
    exif = one(
        "INSERT INTO exif_findings (media_asset_id, finding_type, value_ct) "
        "VALUES (%s, 'gps', %s) RETURNING id",
        media,
        b"\x00",
    )
    conn_acct = one(
        "INSERT INTO connected_accounts (user_id, platform, access_token_ct, status) "
        "VALUES (%s, 'reddit', %s, 'active') RETURNING id",
        user_id,
        b"\x00",
    )
    consent = one(
        "INSERT INTO consents (user_id, purpose, policy_version) "
        "VALUES (%s, 'self_audit', 'v1') RETURNING id",
        user_id,
    )
    run = one(
        "INSERT INTO runs (profile_id, type, status) "
        "VALUES (%s, 'attack', 'succeeded') RETURNING id",
        pid,
    )
    inf = one(
        "INSERT INTO inferences (run_id, profile_id, attribute_code, modality, status, "
        "engine_version) VALUES (%s, %s, 'location', 'text', 'inferred', 'v1') RETURNING id",
        run,
        pid,
    )
    cand = one(
        "INSERT INTO inference_candidates (inference_id, rank) VALUES (%s, 1) RETURNING id", inf
    )
    evidence = one(
        "INSERT INTO inference_evidence (candidate_id, ref_type, ref_id, modality) "
        "VALUES (%s, 'item', %s, 'text') RETURNING id",
        cand,
        item,
    )
    metric = one("INSERT INTO run_metrics (run_id) VALUES (%s) RETURNING id", run)
    remediation = one(
        "INSERT INTO remediations (profile_id, inference_id, run_id, action) "
        "VALUES (%s, %s, %s, 'rewrite') RETURNING id",
        pid,
        inf,
        run,
    )
    return {
        "profiles": pid,
        "import_sources": isrc,
        "items": item,
        "media_assets": media,
        "exif_findings": exif,
        "connected_accounts": conn_acct,
        "consents": consent,
        "runs": run,
        "inferences": inf,
        "inference_candidates": cand,
        "inference_evidence": evidence,
        "run_metrics": metric,
        "remediations": remediation,
    }


@pytest.fixture(scope="module")
def rls_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        yield container


@pytest.fixture(scope="module")
def seeded(rls_container: PostgresContainer) -> dict[str, dict[str, uuid.UUID]]:
    """Migrate (0001+0002) then seed A and B chains as the owner; return their row ids."""
    import os

    os.environ["DATABASE_URL"] = rls_container.get_connection_url(driver="asyncpg")
    get_database_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_database_settings.cache_clear()

    with psycopg.connect(
        host=rls_container.get_container_host_ip(),
        port=int(rls_container.get_exposed_port(5432)),
        user="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        autocommit=True,
    ) as conn:
        cur = conn.cursor()
        # `attributes` are seeded by migration 0005 (incl. 'location', used by the inference chain).
        return {name: _seed_chain(cur, user_id) for name, user_id in _USERS.items()}


@pytest_asyncio.fixture
async def app_engine(rls_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = rls_container.get_container_host_ip()
    port = rls_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


async def _visible_ids(engine: AsyncEngine, user_id: uuid.UUID, table: str) -> set[uuid.UUID]:
    async with engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        result = await conn.execute(text(f"SELECT id FROM {table}"))  # noqa: S608 — fixed names
        return {row[0] for row in result}


@pytest.mark.parametrize("table", _OWNED_TABLES)
async def test_read_isolation(
    app_engine: AsyncEngine, seeded: dict[str, dict[str, uuid.UUID]], table: str
) -> None:
    visible_to_a = await _visible_ids(app_engine, _USERS["A"], table)
    assert seeded["A"][table] in visible_to_a
    assert seeded["B"][table] not in visible_to_a


@pytest.mark.parametrize("table", _OWNED_TABLES)
async def test_fails_closed_without_context(
    app_engine: AsyncEngine, seeded: dict[str, dict[str, uuid.UUID]], table: str
) -> None:
    async with app_engine.connect() as conn, conn.begin():
        result = await conn.execute(text(f"SELECT id FROM {table}"))  # noqa: S608 — fixed names
        assert list(result) == []


async def test_write_isolation_blocks_cross_tenant(
    app_engine: AsyncEngine, seeded: dict[str, dict[str, uuid.UUID]]
) -> None:
    # Scoped to A, try to insert an item into B's scope → WITH CHECK rejects it.
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, _USERS["A"])
        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO items (profile_id, owner_user_id, text_ct, content_hmac) "
                    "VALUES (:pid, :owner, :ct, 'h')"
                ),
                {"pid": seeded["B"]["profiles"], "owner": _USERS["B"], "ct": b"\x00"},
            )


async def test_app_role_cannot_read_data_keys(
    app_engine: AsyncEngine, seeded: dict[str, dict[str, uuid.UUID]]
) -> None:
    async with app_engine.connect() as conn, conn.begin():
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT wrapped_dek FROM data_keys"))
