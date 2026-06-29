"""Data access for `run_metrics` — per-run telemetry (tokens/cost/latency). RLS-scoped.

Metadata only — never content (infra-devops: structured logs/metrics carry no PII). The cost-
optimization loop reads these.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_run_metrics(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    latency_ms: int,
    model_calls: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Record one run's telemetry; token/cost are null until the gateway surfaces usage."""
    await conn.execute(
        text(
            "INSERT INTO run_metrics (run_id, latency_ms, model_calls, prompt_tokens, "
            "completion_tokens, cost_usd) "
            "VALUES (:run_id, :latency, :calls, :ptok, :ctok, :cost)"
        ),
        {
            "run_id": run_id,
            "latency": latency_ms,
            "calls": model_calls,
            "ptok": prompt_tokens,
            "ctok": completion_tokens,
            "cost": cost_usd,
        },
    )
