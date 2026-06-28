"""Application configuration, loaded from the environment (12-factor).

Split into per-concern `BaseSettings` (backend rule: no monolith config); each is cached so
the environment is read once per process. Secrets come from the environment, never hardcoded.
Gateway settings move under pydantic-settings with the M1.5 gateway rebuild.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

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
