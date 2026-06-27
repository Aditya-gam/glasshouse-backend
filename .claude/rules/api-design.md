# Rules — API Design (REST/HTTP)

The HTTP contract. Govern docs + implementation. (≤200 lines.)

## Resource-oriented design (Google AIP)
- **Nouns as resources, verbs as HTTP methods** — `/runs`, `/inferences`; a few **standard methods** (list/get/create/update/delete) cover most operations. *Why:* predictable, learnable, tooling-friendly.
- **Consistent resource names + IDs**; **enums `UPPER_SNAKE_CASE` with a `*_UNSPECIFIED`/`UNKNOWN` zero value.** *Why:* avoids the "0 means a real value" trap.
- **Statelessness** — each request carries its own auth/context; no server session affinity. *Why:* scaling + reliability (12-factor).
- **Correct status codes** — 2xx/4xx/5xx; **`202 Accepted` for async work** (+ `Location`/`run_id`), `201 Created` for creates. *Why:* status is the API's primary machine signal.

## Errors — RFC 9457 `application/problem+json`
The standard, machine-readable error envelope (supersedes RFC 7807, 2023). Members:
- **`type`** — absolute URI identifying the problem type (the primary identifier; default `about:blank`).
- **`status`** — the HTTP status (advisory; the real status line wins).
- **`title`** — short, stable, human summary of the *type*.
- **`detail`** — human explanation of *this occurrence*; **don't parse it for machine data — use extension members.**
- **`instance`** — URI for this specific occurrence.
*Rule:* one consistent error shape; **never leak stack traces/internals**; clients **MUST ignore unknown extension members**.

## Evolution & compatibility
- **URL-path versioning (`/v1/...`)** — *Why:* explicit, visible in logs, obvious to consumers; ship breaking changes without breaking clients.
- **Backward-compatible by default (Zalando/RFC 2119)** — servers may add fields; **clients must ignore unknown fields** (`additionalProperties`). *Why:* lets the API evolve without coordinated upgrades.

## Scale & safety
- **Cursor pagination for large/changing lists** (opaque cursor off a stable key), not `OFFSET`. *Why:* offset re-scans+discards (slow) and skips/dupes under writes.
- **Idempotency keys for non-idempotent writes** — client sends a unique key on `POST`; server dedupes retries. *Why:* `POST` twice = two resources; networks retry. (Mirrors `runs.idempotency_key`.)
- **AuthN + AuthZ on every route; scope every tenant query; re-check resource ownership (no IDOR).** *Why:* Broken Access Control is OWASP A01.

## Contract
- **OpenAPI is the contract, not an afterthought** — generated from the DTOs, published, used for contract tests + client codegen. *Why:* contract-first catches drift early.
- **Async pattern:** `POST → 202 {run_id}`; `GET /runs/{id}` polls (or SSE). *Why:* the right shape for long model work.

## Sources
- [RFC 9457 — Problem Details](https://www.rfc-editor.org/rfc/rfc9457) · [Google AIP](https://google.aip.dev/) · [Zalando RESTful API Guidelines](https://opensource.zalando.com/restful-api-guidelines/) · [Microsoft — Web API design](https://learn.microsoft.com/en-us/azure/architecture/best-practices/api-design).
