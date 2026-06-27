# Rules — Testing

How we prove it works. Govern docs + implementation. (≤200 lines.)

## Shape (Test Pyramid + Testing Trophy)
- **Pyramid (backend)** — *lots* of fast **unit** tests, *some* **integration**, *very few* **end-to-end**. **Avoid the ice-cream cone** (inverted pyramid of slow E2E) — a maintenance nightmare.
- **Trophy (frontend)** — **static** (types/lint) + a *thick* **integration** layer give the best ROI now that higher-level tools are cheap; fewer pure-unit, minimal E2E.
- **Guiding principle (both):** *"the more your tests resemble the way the software is used, the more confidence they give."*

## Principles
- **Test behavior, not implementation** — assert observable outputs, not internal call sequences. *Why:* refactor without rewriting tests.
- **Arrange–Act–Assert** structure every test; one clear behavior per test; descriptive names. *Why:* readable, diagnosable.
- **Push tests down** — if a high-level test fails but no low-level one does, add the low-level test. **Avoid duplication** across layers. *Why:* fast feedback, less redundancy.
- **Narrow integration tests** — one integration point at a time; never hit production systems. *Why:* isolation + speed.
- **Deterministic** — control randomness; for stochastic LLM output assert **semantic equivalence / bounded ranges**, not byte-exact. *Why:* no false failures.

## Mandatory gates (this project)
- **RLS isolation** — every owned table: user A can't see user B's rows.
- **Crypto round-trip + crypto-shred** — encrypt→decrypt works; after DEK delete, ciphertext is unrecoverable.
- **Third-party-drop** — `is_subject_authored=false` never persists.
- **Contract tests** — the backend conforms to its OpenAPI schema (**Schemathesis**); the frontend's **generated client** is drift-checked against the published schema (replaces hand-mirrored DTO checks).
- **Eval-as-CI-gate** — benchmark on a fixed SynthPAI slice; **fail below the accuracy floor** (stops prompt/model regressions).

## Tooling & coverage
- **`pytest`** — **testcontainers** (real Postgres+pgvector, session-scoped) + transaction-rollback per test, `httpx` async client, `app.dependency_overrides` for fakes (not monkeypatch). **Vitest + React Testing Library + MSW** from the user's perspective (**async Server Components → Playwright**, which Vitest can't render). **Static** = `mypy` strict + `ruff` + ESLint/`tsc`.
- **Property-based** — **Schemathesis** fuzzes the API from the OpenAPI schema (conformance / 500s / injection / path-traversal); **Hypothesis** property-tests the pure `domain/` core invariants. *Why:* 10–100× the input coverage of example-based tests, near-zero per-endpoint maintenance.
- **Coverage is a floor, not a goal** — meaningful assertions; don't test trivial getters/setters. *Why:* coverage measures execution, not correctness.

## Sources
- [Martin Fowler / Vocke — Practical Test Pyramid](https://martinfowler.com/articles/practical-test-pyramid.html) · [Kent C. Dodds — Testing Trophy](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications) · [React Testing Library](https://testing-library.com/docs/guiding-principles/) · [pytest](https://docs.pytest.org/).
