# Engineering Rules

Researched, cited engineering standards for this project. They govern **both the build docs (`docs/`) and the eventual implementation**. Each file is concise (**≤200 lines** — longer files degrade adherence) and, for every rule, names the standard, explains what it means, and says why it matters — so it doubles as a readable reference.

**Before writing docs or code in a domain, read its file and follow it.**

| File | Covers |
|---|---|
| `architecture.md` | Clean/Hexagonal dependency rule, 12-factor, AWS Well-Architected, SRE (SLO/error budget) |
| `database.md` | Postgres "Don't Do This" anti-patterns, naming, indexing, multi-tenant RLS, migrations, pooling |
| `backend.md` | FastAPI structure, async/GIL correctness, DI, Pydantic, SQLAlchemy 2.0, errors, resilience |
| `api-design.md` | RFC 9457 problem+json, Google AIP resource-orientation, versioning, cursor pagination, idempotency, OpenAPI |
| `frontend.md` | Next.js Data Access Layer, Server-Action auth/IDOR, taint, CSP, React purity/effects, WCAG, TS strict |
| `security-privacy.md` | OWASP Top 10:2025 + ASVS, deny-by-default, NIST 800-63B auth, GDPR 7 principles + Art. 25, crypto/secrets |
| `infra-devops.md` | 12-factor ops, observability (logs/metrics/traces), CI/CD + eval gate, secrets, least privilege |
| `testing.md` | Test pyramid + trophy, behavior-not-implementation, the mandatory security/eval gates, pytest/RTL |
| `code-style.md` | PEP 8 / Google Python style, type hints + mypy, TS strict + Zod, Conventional Commits |

Prompt-engineering standards (a sibling, domain-specific): `docs/04-ai-engine/prompts/conventions.md`.
