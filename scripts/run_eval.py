"""One-off benchmark eval (M2.2): run the attack engine over the seeded SynthPAI profiles.

Benchmarks the engine (Job 1) — computes top-1/top-3 accuracy per attribute and writes one `eval`
run + `eval_results`. Operator-run against the privileged `DATABASE_URL` (like the seed / a
migration); calls the real LiteLLM gateway, so it is never part of CI (the CI floor gate at M2.6
runs a fixed slice with a cheap/local profile). Run `scripts.seed_synthpai` first.

Usage:
    uv run python -m scripts.run_eval [--limit N]

`--limit N` benchmarks only the first N personas (the fixed, deterministic slice). Requires
`DATABASE_URL` + `MASTER_KEY` in the environment (`.env`).
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_database_settings
from app.db.crypto import get_master_key
from app.gateway.client import GatewayClient
from app.retrieval.embedder import default_embedder
from app.retrieval.pii import default_pii_detector
from app.services.eval import run_eval
from app.services.geocoding import default_geocoder
from app.services.occupation import GatewayOccupationJudge


async def _run(limit: int | None) -> None:
    engine = create_async_engine(get_database_settings().database_url)
    gateway = GatewayClient()
    try:
        async with engine.connect() as conn, conn.begin():
            result = await run_eval(
                conn,
                gateway,
                default_embedder(),
                default_pii_detector(),
                default_geocoder(),
                master_key=get_master_key(),
                judge=GatewayOccupationJudge(gateway),
                limit=limit,
            )
    finally:
        await engine.dispose()
    print(f"eval run {result.run_id} — {result.personas} personas")
    for score in result.scores:
        print(
            f"  {score.attribute:<12} top1={score.top1_acc:.3f} "
            f"top3={score.top3_acc:.3f} (n={score.n})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark the attack engine on SynthPAI (one-off)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="benchmark only the first N personas"
    )
    args = parser.parse_args()
    asyncio.run(_run(args.limit))


if __name__ == "__main__":
    main()
