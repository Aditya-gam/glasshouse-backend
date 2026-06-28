"""R1 acceptance check: the testcontainers Postgres is reachable and the app's extensions load."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_database_reachable(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_required_extensions_installed(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pgcrypto')")
        )
        installed = {row[0] for row in rows}
    assert installed == {"vector", "pgcrypto"}
