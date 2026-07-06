"""Eval floor gate (M2.6) — fail if the latest benchmark dropped below the committed floor.

Reads `eval_floor.json` (the per-attribute top-1 floor, set from the first clean run) and the most
recent `eval` run's `eval_results`, and exits non-zero on any breach. Operator/scheduled-run against
the privileged `DATABASE_URL` **after** a real `run_eval` — it needs real accuracy numbers, so it is
not a per-PR CI job (the deterministic-pipeline gate that runs every PR lives in the test suite).

Usage:
    uv run python -m scripts.eval_gate

An empty floor (`{}`) means the gate is not yet armed (no benchmark has run) → passes with a notice.
"""

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_database_settings
from app.domain.eval_gate import check_eval_floor
from app.repositories import eval_results as eval_results_repo

_FLOOR_PATH = Path(__file__).resolve().parent.parent / "eval_floor.json"


def _load_floor() -> dict[str, float]:
    data = json.loads(_FLOOR_PATH.read_text())
    return {k: float(v) for k, v in data.get("floor", {}).items()}


async def _gate() -> int:
    floor = _load_floor()
    if not floor:
        print("eval floor not armed (eval_floor.json 'floor' is empty) — skipping the gate")
        return 0
    engine = create_async_engine(get_database_settings().database_url)
    try:
        async with engine.connect() as conn:
            actual = await eval_results_repo.latest_eval_top1(conn)
    finally:
        await engine.dispose()
    if not actual:
        print("no eval run found — run `scripts.run_eval` first", file=sys.stderr)
        return 1
    breaches = check_eval_floor(actual, floor)
    for breach in breaches:
        print(
            f"FLOOR BREACH {breach.attribute}: "
            f"top1={breach.top1_acc:.3f} < floor={breach.floor:.3f}",
            file=sys.stderr,
        )
    if breaches:
        print(f"{len(breaches)} attribute(s) below floor — failing the build", file=sys.stderr)
        return 1
    print(f"eval floor OK — {len(floor)} attribute(s) at or above floor")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_gate()))


if __name__ == "__main__":
    main()
