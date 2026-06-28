"""Unit: CORS origins parse from a comma-separated env value (no JSON pre-decoding).

Regression guard — a `list[str]` BaseSettings field is JSON-decoded by pydantic-settings unless
annotated NoDecode, which crashed on `CORS_ALLOW_ORIGINS=http://localhost:3000`. CI has no .env,
so only a real .env (local/deploy) hit it.
"""

import pytest

from app.core.config import CorsSettings


def test_parses_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:3000, https://glasshouse.app")
    assert CorsSettings().allow_origins == ["http://localhost:3000", "https://glasshouse.app"]


def test_parses_single_value_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:3000")
    assert CorsSettings().allow_origins == ["http://localhost:3000"]


def test_default_is_a_list_of_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    origins = CorsSettings().allow_origins
    assert isinstance(origins, list)
    assert "http://localhost:3000" in origins
