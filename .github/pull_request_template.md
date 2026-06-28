## What & why

<!-- What does this change do, and why? Link the task id from docs/11-roadmap/tasks-backend.md. -->

Task:

## Test evidence

<!-- Paste the relevant output: ruff + ruff format + mypy --strict clean, the pytest summary,
Semgrep findings (0 blocking), and the mandatory gate that applies. -->

## Definition of Done

<!-- docs/11-roadmap/definition-of-ready-done.md -->

- [ ] **Behavior** — the acceptance check passes and matches the doc
- [ ] **Tests** — unit (pure `domain/`) and/or narrow integration added; the relevant mandatory gate passes (RLS · crypto round-trip/shred · third-party-drop · contract · eval-floor) where applicable
- [ ] **Static** — `ruff` + `ruff format` + `mypy --strict` clean; no implicit `Any`
- [ ] **Security** — scope-bound query (RLS + app check); no secret/content in logs; consent gate on any run path
- [ ] **Contract** — if it touches the API, `openapi.json` regenerated (the frontend drift-guard) and Schemathesis/contract tests green
- [ ] **Migration** — one Alembic migration per schema change; expand-contract; reversible
- [ ] **Commit** — Conventional Commit, one logical change, references the task id
- [ ] **Docs** — if behavior diverged from the spec, the doc is updated (the spec stays source of truth)
