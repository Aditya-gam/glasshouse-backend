"""Application configuration, loaded from the environment (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend settings. Values come from the environment / `.env` (never hardcoded secrets)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "glasshouse"
    environment: str = "local"

    # Local dev uses the docker-compose Postgres; cloud values come from the environment.
    # `database_url` is the owner/superuser connection (probes, migrations); `app_database_url`
    # is the non-superuser, RLS-enforced role the request path uses (defense-in-depth).
    database_url: str = "postgresql+asyncpg://glasshouse:glasshouse@localhost:5432/glasshouse"
    app_database_url: str = (
        "postgresql+asyncpg://glasshouse_app:glasshouse_app@localhost:5432/glasshouse"
    )

    # Field-encryption master key (env MASTER_KEY). MVP env key → KMS-derived in prod.
    master_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
