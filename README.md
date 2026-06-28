# Glasshouse — Backend

FastAPI service for the **Attack → Measure → Defend** engine. Part of the
[glasshouse](../Privacy-Exposure-App) project — the full spec (`docs/`) and the
UI prototype live in the hub repo.

## Develop
```bash
uv sync
uv run uvicorn app.main:app --reload     # → http://localhost:8000/healthz
uv run pytest
uv run ruff check . && uv run mypy .
```

**Stack:** FastAPI · SQLAlchemy 2.0 (async) + Alembic · `arq` (Redis) workers · pydantic v2 ·
`instructor` · `uv`. Layered (Clean Architecture): `app/{api,services,repositories,domain,workers,db,...}`.
See the hub's `docs/` for the authoritative spec and `docs/11-roadmap/tasks-backend.md` for the build order.
