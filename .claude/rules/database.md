# Rules — Database (PostgreSQL)

Schema, integrity, performance, multi-tenant security. Govern docs + implementation. (≤200 lines.)

## PostgreSQL "Don't Do This" (official anti-patterns → use instead)
- **`timestamptz`, never `timestamp`** — `timestamptz` stores an actual point in time; `timestamp` silently drops the zone. *Why:* the #1 cause of off-by-hours bugs.
- **`text`, never `char(n)`/`varchar(n)`** — use `text` (+ a `CHECK` if you need a limit). *Why:* `char(n)` blank-pads; fixed lengths cause needless migrations.
- **`numeric` (or integer cents), never `money`** — `money` has fixed locale/precision. *Why:* correctness for currency.
- **identity columns, not `serial`** — `GENERATED ... AS IDENTITY`. *Why:* `serial` has ownership/permission footguns.
- **`NOT EXISTS`, not `NOT IN (subquery)`** — *Why:* `NOT IN` returns wrong results with NULLs.
- **`>= x AND < y`, not `BETWEEN` for timestamps** — *Why:* `BETWEEN` is inclusive and double-counts boundaries.
- **`scram-sha-256`, never `trust` over TCP.** **FKs/partitioning, not table inheritance. Triggers, not rules.**

## Naming & modeling
- **snake_case** — tables (we keep singular per repo convention is fine; be consistent), columns singular; `_at` suffix for `timestamptz`, `_date` for `date`; index names `idx_`. *Why:* convention removes guesswork.
- **Normalize first (3NF), denormalize only on a proven bottleneck.** *Why:* normalization prevents update anomalies; premature denormalization rots.
- **Integrity in the DB** — `NOT NULL`, `CHECK`, `UNIQUE`, FKs with deliberate `ON DELETE`, enums. *Why:* the DB is the last line; app code can be bypassed.

## Indexing
- **Index FKs + predicates** — every FK, and columns in `WHERE`/`JOIN`/`ORDER BY`. *Why:* unindexed FKs → slow joins + lock contention.
- **Partial indexes** — index only queried rows (`WHERE deleted_at IS NULL`). *Why:* smaller + faster than full.
- **pgvector HNSW** for similarity; it's RAM-hungry — the one index that scales infra. Monitor `pg_stat_user_indexes`, drop unused. *Why:* indexes slow writes + cost RAM.

## Multi-tenant security
- **Row-Level Security on every tenant table**, keyed on a session GUC (`app.user_id`); **fails closed** (forget a policy → queries return nothing, not everything). Pair with an app-layer scope check (defense-in-depth). *Why:* shared-schema isolation that's safe by construction.
- **Encrypt sensitive columns** (pgcrypto); the key is a **bound parameter**, never string-interpolated. *Why:* prevents key leakage into query logs/backups.
- **Keyed HMAC for dedupe**, not a plain hash (short text is reversible).

## Migrations & connections
- **One migration per change, forward-only, reversible-by-construction; gate cutovers behind flags; never edit a shipped migration.** *Why:* prod data makes destructive rollbacks dangerous.
- **Connection pooling (PgBouncer / async pool)** — each PG connection forks a ~5MB OS process. *Why:* bound connections or exhaust the server.
- **Wrap multi-statement work in transactions.** *Why:* atomicity.

## Sources
- [PostgreSQL — Don't Do This](https://wiki.postgresql.org/wiki/Don%27t_Do_This) · [PostgreSQL docs](https://www.postgresql.org/docs/current/) · [AWS — multi-tenant RLS](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/) · [Crunchy — multi-tenancy](https://www.crunchydata.com/blog/designing-your-postgres-database-for-multi-tenancy).
