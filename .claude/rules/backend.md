# Rules — Backend (FastAPI · Python · SQLAlchemy 2.0)

Server construction. Govern docs + implementation. (≤200 lines.)

## Structure & layering
- **Organize by layer (Clean Architecture)** — `api (routers) → services → repositories → db`, with a pure `domain/` core (no IO). SQL lives **only** in repositories. *Why:* the dependency rule maps directly to folders; the core is reused by API + workers. (This codebase is **layered**, not per-domain-module — see `repo-structure.md`.)
- **Layered + DI** — `routers → services → repositories → db`; inject the DB session, current user, and `require_permission(code)` via `Depends`. *Why:* testable in isolation; one place manages session lifecycle; swap fakes in tests.
- **Thin controllers** — routers validate + delegate; logic lives in services. *Why:* reused by workers.

## Async correctness (the trap)
- **In `async def`, every I/O call must be non-blocking** (async DB driver `asyncpg`, `httpx`). A blocking call (`time.sleep`, sync DB) **freezes the whole event loop**. *Why:* one stall blocks all requests.
- **Blocking I/O → `def` route** (FastAPI runs it in a threadpool). **CPU-bound work → the queue**, not a threadpool (Python's GIL makes threads ineffective for CPU). *Why:* match the tool to the workload.
- **Scoped session per request** — open via DI, apply RLS GUCs, commit/rollback at the edge. *Why:* clean unit-of-work, no leaked transactions.

## Pydantic & validation
- **Validate at the boundary (Pydantic v2)** — parse, don't trust; **per-operation schemas** (create/read/update differ); a custom base model standardizes serialization; **raise `ValueError` in validators** (FastAPI maps it to a 422). *Why:* the wire contract must not leak DB internals.
- **Don't construct response objects manually** — set `response_model`; FastAPI validates + serializes. *Why:* avoids redundant instances + drift.
- **Split `BaseSettings` per module; never hardcode config.** *Why:* 12-factor; no monolith config.

## Types, errors, resilience
- **Fully typed; `mypy` strict; `ruff` lints+formats** (replaces black/isort/flake8). *Why:* catch bugs pre-runtime; one style.
- **Custom exceptions per module → mapped to HTTP at the edge** (problem+json — see `api-design`). *Why:* services stay transport-agnostic.
- **Timeouts + retries-with-backoff + circuit-break on every outbound call** (gateway, KMS); **bounded repair-retry (N≈2) then fail**, never loop. *Why:* a slow dependency must not cascade or burn cost.

## Tasks & security
- **`BackgroundTasks` only for <1s fire-and-forget; everything else → the queue** (retries, scheduling, DLQ). *Why:* in-process tasks die with the request.
- **No secrets in code; never log secrets/keys/decrypted content** — metadata only. *Why:* logs + VCS are top leak paths.

## Testing hooks
- **Async test client (`httpx.AsyncClient` + `ASGITransport`) + `app.dependency_overrides`** for fakes — not monkeypatching internals. *Why:* tests the real wiring, swaps deps cleanly.

## Sources
- [FastAPI docs](https://fastapi.tiangolo.com/) · [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices) · [SQLAlchemy 2.0 async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) · [Pydantic v2](https://docs.pydantic.dev/latest/).
