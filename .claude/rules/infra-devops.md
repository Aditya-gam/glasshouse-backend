# Rules — Infra & DevOps

Deploy, configure, operate. Govern docs + implementation. (≤200 lines.)

## The Twelve-Factor App (deploy/ops factors)
- **Config in the environment** — all config/secrets via env vars; never in code/VCS. *Why:* one build, many envs.
- **Backing services as attached resources** — DB, Redis, KMS, object store, the LiteLLM Proxy are swappable URLs/handles. *Why:* replace a managed service without code change.
- **Build, release, run** — strict separation; immutable build + config = a release. *Why:* reproducible, rollback-able.
- **Stateless processes + disposability** — no in-process state; fast startup, graceful shutdown, idempotent jobs. *Why:* safe to kill/restart/scale anytime.
- **Concurrency via the process model** — scale workers per stage behind the queue. **Dev/prod parity** (containers, same engines). **Logs as event streams** to stdout. *Why:* portability + horizontal scale.

## Observability (three pillars + OpenTelemetry)
- **Metrics, logs, traces** — *metrics* detect a problem, *logs* diagnose what happened, *traces* show where/how. *Why:* you can't operate what you can't see.
- **Correlate via shared IDs** — put `trace_id`/`span_id` + `request_id` in every log line. *Why:* pivot between the three pillars in one investigation.
- **Structured (JSON) logs with context** — `user_id`, `request_id`, `service`, `version`, `run_id`; **never content, keys, or PII**. *Why:* machine-readable + privacy-safe (here `run_metrics` = tokens/cost/latency, the cost optimize loop).

## CI/CD
- **Gate deploys on tests + the eval floor** — a regression (unit/integration/RLS/crypto, or SynthPAI accuracy below floor) fails the build. *Why:* prevent silent regressions, incl. prompt/model drift.
- **Migrations in the release step** (`alembic upgrade head` before traffic). **Small, frequent, reversible deploys** (AWS Well-Architected / SRE). **Pinned, reproducible builds.**

## Secrets & least privilege
- **Secret manager / env injection; rotate** — the proxy holds provider keys, the app holds only a virtual key. **Least-privilege IAM** — each service gets only what it needs (app DB role can't read `data_keys`). *Why:* minimize secret sprawl + blast radius.

## Sources
- [The Twelve-Factor App](https://12factor.net/) · [OpenTelemetry — Observability primer](https://opentelemetry.io/docs/concepts/observability-primer/) · [Google SRE](https://sre.google/sre-book/table-of-contents/) · [AWS Well-Architected — Operational Excellence](https://docs.aws.amazon.com/wellarchitected/latest/operational-excellence-pillar/welcome.html).
