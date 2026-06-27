# Rules — Architecture

System-design standards. Govern the build docs **and** the implementation. Each rule: the standard, what it means, why it matters. (≤200 lines.)

## Dependencies & layering (Clean / Hexagonal)
- **The Dependency Rule** (Clean Architecture, R.C. Martin) — source dependencies point **inward only**; the domain knows nothing about infrastructure. *Why:* business logic survives DB/framework/gateway swaps. Keep `api → services → repositories → db`; nothing lower imports anything higher.
- **Ports & Adapters** (Cockburn, Hexagonal) — the app defines interfaces by what it *needs*; adapters (DB, gateway, connectors) conform to them. *Why:* loose coupling + trivial test fakes.
- **Pure domain core** — domain logic (matching, scoring, clustering, ablation, noise) has **no I/O**. *Why:* fast, deterministic unit tests; reused by API and workers.
- **One code path per behavior** — workers call the *same services* as the API. *Why:* no logic drifting out of sync.

## The Twelve-Factor App (the factors we enforce)
- **III Config in the environment** — config/secrets via env, never in code. **IV Backing services as attached resources** — DB/Redis/KMS/object-store/proxy are swappable handles. **V Build/release/run** separated. **VI Stateless processes** — push state to backing services. **VIII Concurrency** via the process model (scale workers per stage). **IX Disposability** — fast start, graceful shutdown, idempotent jobs. **X Dev/prod parity**. **XI Logs as event streams** (stdout). *Why:* the portability + horizontal-scale baseline for cloud apps.

## Runtime properties
- **Async + queue-decoupled** — long work goes through the queue; API returns a handle, client polls. *Why:* decouples request latency from slow model latency; enables retries/backpressure.
- **Idempotency** — every enqueue/mutation carries an idempotency key. *Why:* at-least-once queues + client retries are inevitable; double-apply is a bug.
- **Fail closed** — on ambiguity (authz, authorship, consent) deny. *Why:* a missing check must block, not leak.

## AWS Well-Architected (the six-pillar lens)
Review designs against **Operational Excellence, Security, Reliability, Performance Efficiency, Cost Optimization, Sustainability**. Key principles we adopt: **make small, frequent, reversible changes · anticipate failure · implement observability · use managed services**. *Why:* a structured checklist beats ad-hoc judgment.

## Reliability (Google SRE)
- **SLOs over "100% uptime"** — pick a target < 100% and an **error budget** (allowed unreliability). *Why:* 100% is the wrong target for everything; the budget de-politicizes the speed-vs-stability call.
- **Minimize toil** — automate repetitive ops. *Why:* toil doesn't scale and crowds out engineering.

## Boundaries & evolvability
- **Explicit trust boundaries** — name every egress (sub-processors), decryption point, and public surface. *Why:* you can only protect a boundary you've drawn.
- **Single egress chokepoint** for model calls. *Why:* one place for cost caps, no-logging, routing, the privacy boundary.
- **Least privilege between services** — app DB role can't read key material. *Why:* contains compromise.
- **ADRs** — record context/decision/rationale/consequences for every significant choice. *Why:* future-you needs the "why".
- **Design for the known seam, don't build it** (here: org/RBAC for the future enterprise tier). *Why:* cheap optionality vs expensive rewrite.

## Sources
- [Clean Architecture / Dependency Rule](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html) · [Hexagonal (Cockburn)](https://alistair.cockburn.us/hexagonal-architecture/) · [The Twelve-Factor App](https://12factor.net/) · [AWS Well-Architected](https://docs.aws.amazon.com/wellarchitected/latest/framework/the-pillars-of-the-framework.html) · [Google SRE Book](https://sre.google/sre-book/table-of-contents/).
