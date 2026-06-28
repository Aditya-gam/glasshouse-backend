"""M0.6 — Clerk JWT verification + the current-user dependency (valid → user, invalid → 401).

`verify_token` is unit-tested with a real RS256 keypair and a mocked JWKS. The dependency is
exercised over HTTP through a stub endpoint (`GET /v1/imports`): auth passing reaches the 501
stub; auth failing returns 401.
"""

import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import jwt
import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.api.deps import get_owner_engine
from app.auth import clerk
from app.core.config import get_auth_settings, get_database_settings
from app.main import app

_ISSUER = "https://clerk.test"


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private_pem, key.public_key()


def _make_jwt(private_pem: bytes, *, sub: str, iss: str = _ISSUER, exp_delta: int = 3600) -> str:
    now = int(time.time())
    payload = {"sub": sub, "iat": now, "exp": now + exp_delta, "iss": iss}
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "k1"})


class _FakeJWKS:
    def __init__(self, public_key: RSAPublicKey) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token: str) -> object:
        return type("Key", (), {"key": self._key})()


@pytest.fixture
def _clerk_configured(
    keypair: tuple[bytes, RSAPublicKey], monkeypatch: pytest.MonkeyPatch
) -> Iterator[bytes]:
    private_pem, public_key = keypair
    monkeypatch.setenv("CLERK_JWKS_URL", "https://example.test/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_ISSUER", _ISSUER)
    get_auth_settings.cache_clear()
    monkeypatch.setattr(clerk, "_jwks_client", lambda url: _FakeJWKS(public_key))
    yield private_pem
    get_auth_settings.cache_clear()


def test_verify_token_returns_claims(_clerk_configured: bytes) -> None:
    claims = clerk.verify_token(_make_jwt(_clerk_configured, sub="user_abc"))
    assert claims.clerk_user_id == "user_abc"


def test_verify_token_rejects_expired(_clerk_configured: bytes) -> None:
    with pytest.raises(clerk.AuthError):
        clerk.verify_token(_make_jwt(_clerk_configured, sub="user_abc", exp_delta=-10))


def test_verify_token_rejects_wrong_issuer(_clerk_configured: bytes) -> None:
    with pytest.raises(clerk.AuthError):
        clerk.verify_token(_make_jwt(_clerk_configured, sub="user_abc", iss="https://evil.test"))


def test_verify_token_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    get_auth_settings.cache_clear()
    with pytest.raises(clerk.AuthError):
        clerk.verify_token("any.token.here")
    get_auth_settings.cache_clear()


# --------------------------------------------------- the dependency (HTTP) --


@pytest.fixture(scope="module")
def auth_container() -> Iterator[tuple[PostgresContainer, uuid.UUID, str]]:
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
        clerk_user_id = "user_seeded"
        with psycopg.connect(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user="glasshouse",
            password="glasshouse",
            dbname="glasshouse",
            autocommit=True,
        ) as conn:
            row = conn.execute(
                "INSERT INTO users (clerk_user_id) VALUES (%s) RETURNING id", (clerk_user_id,)
            ).fetchone()
            assert row is not None
        yield container, row[0], clerk_user_id


@pytest_asyncio.fixture
async def client(
    auth_container: tuple[PostgresContainer, uuid.UUID, str],
) -> AsyncIterator[AsyncClient]:
    container, _, _ = auth_container
    engine = create_async_engine(container.get_connection_url(driver="asyncpg"))
    app.dependency_overrides[get_owner_engine] = lambda: engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_bearer_jwt_resolves_user(
    client: AsyncClient,
    auth_container: tuple[PostgresContainer, uuid.UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, clerk_user_id = auth_container
    monkeypatch.setattr(clerk, "verify_token", lambda token: clerk.ClerkClaims(clerk_user_id, None))
    resp = await client.get("/v1/imports", headers={"Authorization": "Bearer x.y.z"})
    assert resp.status_code == 501  # auth passed → the contract-first stub


async def test_invalid_bearer_is_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(token: str) -> clerk.ClerkClaims:
        raise clerk.AuthError("bad")

    monkeypatch.setattr(clerk, "verify_token", _raise)
    resp = await client.get("/v1/imports", headers={"Authorization": "Bearer bad"})
    assert resp.status_code == 401


async def test_unknown_user_is_401(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        clerk, "verify_token", lambda token: clerk.ClerkClaims("user_missing", None)
    )
    resp = await client.get("/v1/imports", headers={"Authorization": "Bearer x.y.z"})
    assert resp.status_code == 401


async def test_no_credentials_is_401(client: AsyncClient) -> None:
    assert (await client.get("/v1/imports")).status_code == 401


async def test_dev_header_fallback_still_works(client: AsyncClient) -> None:
    resp = await client.get("/v1/imports", headers={"X-Dev-User-Id": str(uuid.uuid4())})
    assert resp.status_code == 501  # non-prod dev fallback → the stub
