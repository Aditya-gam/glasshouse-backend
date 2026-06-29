"""Unit (M1.9): get_arq_pool — returns the cached pool, or 503 when Redis is unreachable."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.deps import get_arq_pool


def _request(**state: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


async def test_returns_cached_pool() -> None:
    sentinel = object()
    assert await get_arq_pool(_request(arq_pool=sentinel)) is sentinel  # type: ignore[arg-type]


async def test_503_when_pool_cannot_be_created(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> object:
        raise OSError("no redis")

    monkeypatch.setattr("app.workers.queue.create_arq_pool", _boom)
    with pytest.raises(HTTPException) as exc:
        await get_arq_pool(_request())  # type: ignore[arg-type]  # no arq_pool on state → create
    assert exc.value.status_code == 503
