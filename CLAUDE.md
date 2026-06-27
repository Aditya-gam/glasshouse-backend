# CLAUDE.md — Glasshouse Backend

The FastAPI backend for **Glasshouse** (privacy self-audit: **Attack → Measure → Defend**).

## Where the spec lives
The authoritative spec is the **hub repo** (`../Privacy-Exposure-App/docs/`, GitHub: `glasshouse`). Read for any task:
- `docs/11-roadmap/tasks-backend.md` — the build order (tracer bullet → M0–M7).
- `docs/02-architecture/*`, `docs/03-data/*`, `docs/04-ai-engine/*`, `docs/05-backend/*`, `docs/06-api/*` — the design.
- `docs/00-traceability.md` — the change-trigger map; consult before cross-cutting edits.

## Engineering rules
Follow `.claude/rules/*` (copied from the hub) — architecture · backend · database · api-design · security-privacy · testing · code-style · infra-devops. **SDE-2/3 bar; `ruff` + `mypy --strict` clean; one conventional commit per task.**

## Stack & layout
FastAPI · SQLAlchemy 2.0 async + Alembic · `arq` (Redis) · pydantic v2 · `instructor` · `uv`. **Layered (Clean Architecture):** `app/{api,services,repositories,domain,workers,db,auth,gateway,ingestion,retrieval}`. SQL lives **only** in repositories; the pure `domain/` has no IO.

## Commands
```
uv sync
uv run uvicorn app.main:app --reload          # → http://localhost:8000/healthz
uv run pytest                                 # testcontainers Postgres
uv run ruff check . && uv run mypy .
alembic upgrade head
arq app.workers.queue.WorkerSettings
```

## Critical (non-negotiable)
- **Never log** secrets/keys/decrypted content. `.env` git-ignored; the LiteLLM proxy holds provider keys — **never the app**.
- Every tenant query scoped by **RLS + an app check**; fail **closed** on auth/consent/ambiguity.
- **Calibrated reliability only** (point + interval) — never raw model confidence.
- Drop third-party-authored content at ingestion (`is_subject_authored=false` → discard).
