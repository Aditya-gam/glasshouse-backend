"""Async database engines + request-scoped session.

Two roles (defense-in-depth): `engine` is the owner/superuser (the readiness probe, and the
Alembic target at M0.4); `app_engine` is the non-superuser, RLS-enforced role the request path
and workers (M1.9) bind to. Both pools are disposed on shutdown via the app lifespan (12-factor
disposability). The request-scoped session is the repositories' unit of work.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_database_settings

_db = get_database_settings()

# Owner/superuser engine — readiness probe; Alembic uses its own connection (M0.4).
engine = create_async_engine(_db.database_url, pool_pre_ping=True)
# RLS-enforced application-role engine — the request path + workers bind here.
app_engine = create_async_engine(_db.app_database_url, pool_pre_ping=True)

_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped owner session; commit at the edge, roll back on error."""
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    """Dispose both engines' connection pools on shutdown (lifespan-managed)."""
    await engine.dispose()
    await app_engine.dispose()
