"""One-off benchmark seed (M2.1): SynthPAI → benchmark profiles / items / eval_labels.

Downloads `RobinSta/SynthPAI` (CC BY-NC-SA 4.0; ~7.8k synthetic comments, ~300 personas) from
Hugging Face and seeds it through the normal ingestion path under the benchmark system user.
Operator-run against the privileged `DATABASE_URL` (like a migration) — never part of CI or the
request path. Idempotent: re-running dedupes items and upserts labels.

Usage:
    uv run python -m scripts.seed_synthpai [--limit N]

`--limit N` seeds only the first N personas (a fixed, deterministic slice — the dev/CI-gate cut).
Requires `DATABASE_URL` + `MASTER_KEY` in the environment (`.env`).
"""

import argparse
import asyncio
import json
from typing import Any

from huggingface_hub import hf_hub_download
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_database_settings
from app.db.crypto import get_master_key
from app.ingestion.sources.synthpai import SynthPaiPersona, parse_synthpai_rows
from app.retrieval.embedder import default_embedder
from app.services.benchmark import seed_synthpai

_REPO_ID = "RobinSta/SynthPAI"
_FILENAME = "synthpai.jsonl"
# Pinned dataset commit (supply-chain rule A03): re-seeding is reproducible and a mutated
# upstream can't silently rewrite the ground truth. Bump deliberately, then re-seed + re-benchmark.
_REVISION = "b572595f543a51db789caddbb81a9fc4edc6c32f"


def _load_personas(limit: int | None) -> list[SynthPaiPersona]:
    path = hf_hub_download(
        repo_id=_REPO_ID, filename=_FILENAME, repo_type="dataset", revision=_REVISION
    )
    with open(path, encoding="utf-8") as handle:
        rows: list[dict[str, Any]] = [json.loads(line) for line in handle if line.strip()]
    personas = parse_synthpai_rows(rows)
    return personas[:limit] if limit is not None else personas


async def _seed(personas: list[SynthPaiPersona]) -> None:
    engine = create_async_engine(get_database_settings().database_url)
    try:
        async with engine.connect() as conn, conn.begin():
            result = await seed_synthpai(
                conn, default_embedder(), personas, master_key=get_master_key()
            )
    finally:
        await engine.dispose()
    print(
        f"seeded {result.personas} personas: {result.items_inserted} items inserted, "
        f"{result.items_deduped} deduped, {result.labels_upserted} labels upserted"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the SynthPAI text benchmark (one-off).")
    parser.add_argument("--limit", type=int, default=None, help="seed only the first N personas")
    args = parser.parse_args()
    personas = _load_personas(args.limit)
    asyncio.run(_seed(personas))


if __name__ == "__main__":
    main()
