"""Integration (M5.2): GET /v1/inferences/{id} — the attribution detail (tenant-scoped).

Real Alembic schema, app-role + RLS. Seeds an inference + ranked candidates + evidence (and the
source item) directly, then asserts the endpoint decrypts and assembles the finding, masks Art. 9
without consent (fail closed), 404s another user's id (no IDOR), and surfaces its id on the
dashboard card. Content is decrypted in-query and never leaves Postgres in a plaintext column.
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_app_engine
from app.api.v1.inferences import get_inference
from app.core.config import get_database_settings
from app.db.crypto import provision_user_dek
from app.db.rls import set_rls_context
from app.domain.output_schema import GeoHierValue
from app.main import app
from app.repositories import inferences as inferences_repo
from app.repositories.profiles import get_or_create_self_profile
from app.repositories.runs import insert_run_v2

_MASTER_KEY = "test-master-key-not-a-real-secret"
_SEATTLE = GeoHierValue(
    city="Seattle", region="WA", country="USA", precision_level="city", neighborhood="Capitol Hill"
)
_BELLEVUE = GeoHierValue(city="Bellevue", region="WA", country="USA", precision_level="city")
_DAMASCUS = GeoHierValue(city="Damascus", country="Syria", precision_level="city")


@pytest.fixture(scope="module")
def detail_container() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        image="pgvector/pgvector:pg16",
        username="glasshouse",
        password="glasshouse",
        dbname="glasshouse",
        driver="psycopg",
    ) as container:
        os.environ["DATABASE_URL"] = container.get_connection_url(driver="asyncpg")
        os.environ["MASTER_KEY"] = _MASTER_KEY
        get_database_settings.cache_clear()
        try:
            command.upgrade(Config("alembic.ini"), "head")
        finally:
            get_database_settings.cache_clear()
        yield container


@pytest_asyncio.fixture
async def owner_engine(detail_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(detail_container.get_connection_url(driver="asyncpg"))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(detail_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    host = detail_container.get_container_host_ip()
    port = detail_container.get_exposed_port(5432)
    url = f"postgresql+asyncpg://glasshouse_app:glasshouse_app@{host}:{port}/glasshouse"
    engine = create_async_engine(url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr("app.api.v1.inferences.get_master_key", lambda: _MASTER_KEY)
    app.dependency_overrides[get_app_engine] = lambda: app_engine
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_user(owner_engine: AsyncEngine) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        user_id: uuid.UUID = (
            await conn.execute(text("INSERT INTO users DEFAULT VALUES RETURNING id"))
        ).scalar_one()
        await provision_user_dek(conn, user_id, _MASTER_KEY)
    return user_id


async def _grant_art9(owner_engine: AsyncEngine, user_id: uuid.UUID) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO consents (user_id, purpose, special_category, policy_version) "
                "VALUES (:u, 'art9_inference', true, 'v1')"
            ),
            {"u": user_id},
        )


async def _seed_finding(
    app_engine: AsyncEngine,
    user_id: uuid.UUID,
    *,
    attribute: str = "location",
    is_art9: bool = False,
    reasoning_reveals_art9: bool = False,
    value: GeoHierValue = _SEATTLE,
    alt_value: GeoHierValue | None = _BELLEVUE,
    reasoning: str = "They mention a local landmark.",
    with_evidence: bool = True,
    item_text: str = "Grabbed coffee near Pike Place this morning.",
    quote: str = "near Pike Place",
) -> uuid.UUID:
    """Seed one inference + ranked candidates (+ evidence and its source item); returns its id."""
    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        profile_id = await get_or_create_self_profile(conn, user_id)
        run_id = await insert_run_v2(
            conn, profile_id, run_type="attack", status="succeeded", engine_version="attack_text_v1"
        )
        item_id: uuid.UUID | None = None
        if with_evidence:
            source_id = (
                await conn.execute(
                    text(
                        "INSERT INTO import_sources (profile_id, platform, method) "
                        "VALUES (:p, 'reddit', 'connector') RETURNING id"
                    ),
                    {"p": profile_id},
                )
            ).scalar_one()
            item_id = (
                await conn.execute(
                    text(
                        "INSERT INTO items (profile_id, owner_user_id, import_source_id, text_ct, "
                        "content_hmac, posted_at) VALUES (:p, :o, :src, "
                        "encrypt_field(:o, :txt, :mk), :hmac, "
                        "'2024-05-01T12:00:00+00') RETURNING id"
                    ),
                    {
                        "p": profile_id,
                        "o": user_id,
                        "src": source_id,
                        "txt": item_text,
                        "mk": _MASTER_KEY,
                        "hmac": f"hmac-{item_text[:24]}",
                    },
                )
            ).scalar_one()
        inference_id = await inferences_repo.insert_inference_v2(
            conn,
            run_id=run_id,
            profile_id=profile_id,
            owner_user_id=user_id,
            attribute_code=attribute,
            status="inferred",
            engine_version="attack_text_v1",
            reasoning=reasoning,
            reasoning_reveals_art9=reasoning_reveals_art9,
            master_key=_MASTER_KEY,
        )
        candidate_id = await inferences_repo.insert_candidate(
            conn,
            inference_id=inference_id,
            rank=1,
            value_json=value.model_dump_json(),
            raw_confidence=0.9,
            confidence_source="self_consistency",
            calibrated_reliability=Decimal("0.76"),
            owner_user_id=user_id,
            is_art9=is_art9,
            master_key=_MASTER_KEY,
        )
        if alt_value is not None:
            await inferences_repo.insert_candidate(
                conn,
                inference_id=inference_id,
                rank=2,
                value_json=alt_value.model_dump_json(),
                raw_confidence=0.4,
                confidence_source="self_consistency",
                calibrated_reliability=Decimal("0.40"),
                owner_user_id=user_id,
                is_art9=is_art9,
                master_key=_MASTER_KEY,
            )
        if item_id is not None:
            await inferences_repo.insert_evidence(
                conn,
                candidate_id=candidate_id,
                ref_type="item",
                ref_id=item_id,
                span_json=json.dumps({"quote": quote}),
                rationale="mentions the neighborhood",
                owner_user_id=user_id,
                master_key=_MASTER_KEY,
            )
    return inference_id


def _headers(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id)}


async def test_detail_serves_candidates_and_decrypted_evidence(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_finding(
        app_engine, user_id, reasoning="They mention Pike Place Market."
    )

    resp = await client.get(f"/v1/inferences/{inference_id}", headers=_headers(user_id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "location"
    assert body["value"] == "Seattle, WA, USA"
    assert body["reliability"]["point"] == 0.76
    assert body["precision"] == "city"
    assert body["neighborhood"] == "Capitol Hill"
    assert body["reasoning"] == "They mention Pike Place Market."
    # ranked candidates carry a calibrated (never raw) note
    assert [c["rank"] for c in body["candidates"]] == [1, 2]
    assert body["candidates"][0]["label"] == "Seattle, WA, USA"
    assert body["candidates"][0]["note"] == "76% reliable"
    assert body["candidates"][1]["label"] == "Bellevue, WA, USA"
    # one decrypted, per-item evidence row joined to its source post
    assert len(body["evidence_items"]) == 1
    evidence = body["evidence_items"][0]
    assert evidence["kind"] == "likely"  # attack-side; ablation ("proven") is defend-side
    assert evidence["type"] == "text"
    assert evidence["source"] == "reddit"
    assert evidence["date"] == "2024-05-01"
    assert evidence["text"] == "Grabbed coffee near Pike Place this morning."
    assert evidence["spans"] == ["near Pike Place"]
    assert evidence["rationale"] == "mentions the neighborhood"


async def test_art9_value_masked_without_consent(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)  # no art9_inference consent
    inference_id = await _seed_finding(
        app_engine,
        user_id,
        attribute="birthplace",
        is_art9=True,
        reasoning_reveals_art9=True,
        value=_DAMASCUS,
        alt_value=None,
        reasoning="They mention being born in Damascus.",
        with_evidence=False,
    )

    resp = await client.get(f"/v1/inferences/{inference_id}", headers=_headers(user_id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["art9"] is True
    assert body["value"] is None  # the special-category value is masked...
    assert body["candidates"][0]["label"] == ""  # ...including the candidate's rendered value...
    assert body["reasoning"] == ""  # ...and the Art. 9-revealing reasoning is withheld
    # the reliability is not the sensitive value, so it is still shown
    assert body["reliability"]["point"] == 0.76


async def test_art9_value_shown_with_consent(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    await _grant_art9(owner_engine, user_id)
    inference_id = await _seed_finding(
        app_engine,
        user_id,
        attribute="birthplace",
        is_art9=True,
        reasoning_reveals_art9=True,
        value=_DAMASCUS,
        alt_value=None,
        reasoning="They mention being born in Damascus.",
        with_evidence=False,
    )

    resp = await client.get(f"/v1/inferences/{inference_id}", headers=_headers(user_id))

    body = resp.json()
    assert body["value"] == "Damascus, Syria"  # decrypted under valid Art. 9 consent
    assert body["candidates"][0]["label"] == "Damascus, Syria"
    assert body["reasoning"] == "They mention being born in Damascus."


async def test_another_users_inference_is_404_no_idor(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    owner = await _seed_user(owner_engine)
    other = await _seed_user(owner_engine)
    inference_id = await _seed_finding(app_engine, owner)

    resp = await client.get(f"/v1/inferences/{inference_id}", headers=_headers(other))

    assert resp.status_code == 404  # RLS hides it; no cross-tenant read


async def test_missing_inference_is_404(client: AsyncClient, owner_engine: AsyncEngine) -> None:
    user_id = await _seed_user(owner_engine)

    resp = await client.get(f"/v1/inferences/{uuid.uuid4()}", headers=_headers(user_id))

    assert resp.status_code == 404


async def test_dashboard_card_carries_the_inference_id(
    client: AsyncClient, owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_finding(app_engine, user_id)

    resp = await client.get("/v1/inferences", headers=_headers(user_id))

    card = next(c for c in resp.json() if c["code"] == "location")
    assert card["id"] == str(inference_id)  # the card links to GET /v1/inferences/{id}


async def test_detail_handler_directly_for_coverage(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the endpoint body runs under httpx-ASGI (untraced by CI coverage); call it directly too.
    monkeypatch.setattr("app.api.v1.inferences.get_master_key", lambda: _MASTER_KEY)
    user_id = await _seed_user(owner_engine)
    inference_id = await _seed_finding(app_engine, user_id)

    async with app_engine.connect() as conn, conn.begin():
        await set_rls_context(conn, user_id)
        finding = await get_inference(inference_id, conn)

    assert finding.code == "location"
    assert finding.value == "Seattle, WA, USA"
    assert finding.evidence_items[0].text == "Grabbed coffee near Pike Place this morning."
