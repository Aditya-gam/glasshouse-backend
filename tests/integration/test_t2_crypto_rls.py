"""T2 acceptance — the data-layer tracer bullet.

Proves the security foundation end to end against a real Postgres:
  1. crypto round-trip            — encrypt on insert, decrypt on read, same plaintext
  2. RLS isolation (read + write) — B can't see or spoof A's rows; unscoped → nothing
  3. data_keys lockout            — the app role cannot read key material
  4. crypto-shred                 — drop the DEK → the ciphertext is unrecoverable
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.rls import set_rls_context
from app.repositories.inferences import get_inference_reasoning, insert_inference
from app.repositories.items import get_item_text, insert_item, list_item_ids

SeedUser = Callable[[], Awaitable[UUID]]


async def test_item_round_trips_for_owner(
    app_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        item_id = await insert_item(conn, user_a, "I live in Riverside, CA", master_key)
        assert await get_item_text(conn, item_id, master_key) == "I live in Riverside, CA"


async def test_item_invisible_to_other_user(
    app_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    user_b = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        item_id = await insert_item(conn, user_a, "secret", master_key)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_b)
        assert await get_item_text(conn, item_id, master_key) is None
        assert await list_item_ids(conn) == []


async def test_write_for_other_user_blocked(
    app_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    user_b = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        # Inserting a row owned by B while scoped to A violates the policy's WITH CHECK.
        with pytest.raises(DBAPIError):
            await insert_item(conn, user_b, "spoof", master_key)


async def test_fails_closed_without_context(
    app_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        await insert_item(conn, user_a, "secret", master_key)

    # No RLS context set → an unscoped session matches no rows (fail-closed).
    async with app_engine.connect() as conn, conn.begin():
        assert await list_item_ids(conn) == []


async def test_app_role_cannot_read_data_keys(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn, conn.begin():
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT wrapped_dek FROM data_keys"))


async def test_crypto_shred_makes_ciphertext_unrecoverable(
    app_engine: AsyncEngine, owner_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        item_id = await insert_item(conn, user_a, "secret", master_key)

    # Crypto-shred: delete the DEK (owner). The ciphertext now cannot be decrypted.
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM data_keys WHERE user_id = :uid"), {"uid": user_a})

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        with pytest.raises(DBAPIError):
            await get_item_text(conn, item_id, master_key)


async def test_inference_round_trips_and_is_isolated(
    app_engine: AsyncEngine, seed_user: SeedUser, master_key: str
) -> None:
    user_a = await seed_user()
    user_b = await seed_user()
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_a)
        inf_id = await insert_inference(conn, user_a, "city", "lives near a named park", master_key)
        assert await get_inference_reasoning(conn, inf_id, master_key) == "lives near a named park"

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_b)
        assert await get_inference_reasoning(conn, inf_id, master_key) is None
