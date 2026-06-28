"""Application configuration, loaded from the environment (12-factor).

Split into per-concern `BaseSettings` (backend rule: no monolith config); each is cached so
the environment is read once per process. Secrets come from the environment, never hardcoded.
Gateway settings move under pydantic-settings with the M1.5 gateway rebuild.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_ENV = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class AppSettings(BaseSettings):
    """General process identity + environment."""

    model_config = _ENV

    app_name: str = "glasshouse"
    environment: str = "local"


class DatabaseSettings(BaseSettings):
    """Postgres connections. Two roles: owner (probes/migrations) and the RLS-enforced app role."""

    model_config = _ENV

    database_url: str = "postgresql+asyncpg://glasshouse:glasshouse@localhost:5432/glasshouse"
    app_database_url: str = (
        "postgresql+asyncpg://glasshouse_app:glasshouse_app@localhost:5432/glasshouse"
    )


class CryptoSettings(BaseSettings):
    """Field-encryption master key (env MASTER_KEY). MVP env key → KMS-derived in prod."""

    model_config = _ENV

    master_key: str | None = None


class AuthSettings(BaseSettings):
    """Clerk JWT verification. JWKS is cached; issuer/audience verified when configured."""

    model_config = _ENV

    clerk_jwks_url: str | None = None
    clerk_issuer: str | None = None
    clerk_audience: str | None = None
    clerk_webhook_secret: str | None = None


class CorsSettings(BaseSettings):
    """Browser CORS for the Next.js frontend. Origins are an allowlist (never ``*`` with creds)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="CORS_", extra="ignore"
    )

    # NoDecode: take the raw env string, don't let pydantic-settings JSON-decode it first — the
    # validator below splits a comma-separated list (CORS_ALLOW_ORIGINS=http://a,https://b).
    allow_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator("allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


class GatewaySettings(BaseSettings):
    """LiteLLM Proxy egress (the single model chokepoint + privacy boundary).

    The app holds ONLY the proxy's virtual key — all provider keys live in the proxy (CLAUDE.md).
    Slots resolve to concrete models per profile (local|cloud); see `app.gateway.slots`.
    """

    model_config = _ENV

    litellm_base_url: str = "http://localhost:4000"
    litellm_virtual_key: str = "sk-local-dev"  # dev placeholder; the budgeted virtual key in prod
    gateway_profile: str = "local"  # local (Ollama, $0) | cloud (cited runs)
    gateway_max_retries: int = 2  # bounded repair-retry (llm-gateway.md) — never an infinite loop


@lru_cache
def get_app_settings() -> AppSettings:
    return AppSettings()


@lru_cache
def get_database_settings() -> DatabaseSettings:
    return DatabaseSettings()


@lru_cache
def get_crypto_settings() -> CryptoSettings:
    return CryptoSettings()


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()


@lru_cache
def get_cors_settings() -> CorsSettings:
    return CorsSettings()


@lru_cache
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
