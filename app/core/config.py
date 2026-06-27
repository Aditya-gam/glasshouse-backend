"""Application configuration, loaded from the environment (12-factor)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Backend settings. Values come from the environment / `.env` (never hardcoded secrets)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "inference-exposure-auditor"
    environment: str = "local"

    # Local dev uses the docker-compose Postgres; cloud values come from the environment.
    database_url: str = "postgresql+asyncpg://iea:iea@localhost:5432/iea"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
