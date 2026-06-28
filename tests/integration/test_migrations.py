"""M0.4 acceptance — `alembic upgrade head` builds the full v2 schema on a fresh database.

Dedicated container (so it doesn't collide with the T2 tracer schema on the shared one).
"""

from collections.abc import Iterator

import psycopg
import pytest
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.core.config import get_database_settings

_CORE_TABLES = {
    "users",
    "organizations",
    "memberships",
    "permissions",
    "role_permissions",
    "data_keys",
    "consents",
    "profiles",
    "connected_accounts",
    "import_sources",
    "items",
    "media_assets",
    "exif_findings",
    "attributes",
    "runs",
    "inferences",
    "inference_candidates",
    "inference_evidence",
    "run_metrics",
    "eval_labels",
    "eval_results",
    "calibration",
    "remediations",
    "audit_log",
    "alembic_version",
}
_CRYPTO_FUNCTIONS = {"encrypt_field", "decrypt_field", "provision_user_dek"}


@pytest.fixture(scope="module")
def migration_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        yield container


def test_upgrade_head_builds_schema(
    migration_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", migration_container.get_connection_url(driver="asyncpg"))
    get_database_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_database_settings.cache_clear()

    with psycopg.connect(
        host=migration_container.get_container_host_ip(),
        port=int(migration_container.get_exposed_port(5432)),
        user="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
    ) as conn:
        tables = {
            r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        functions = {
            r[0]
            for r in conn.execute(
                "SELECT proname FROM pg_proc WHERE proname = ANY(%s)", ([*_CRYPTO_FUNCTIONS],)
            )
        }
        role = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'glasshouse_app'").fetchone()
        hnsw = conn.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_items_embedding_hnsw'"
        ).fetchone()

    assert tables >= _CORE_TABLES, f"missing: {sorted(_CORE_TABLES - tables)}"
    assert functions == _CRYPTO_FUNCTIONS
    assert role is not None
    assert hnsw is not None
