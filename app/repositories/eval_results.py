"""Data access for `eval_results` — per-attribute benchmark accuracy for one eval run (Job 1).

Written on a **privileged** connection by the eval service (M2.2). Feeds the CI floor gate and the
public accuracy/trust number; no data subject, so no encryption.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def insert_eval_result(
    conn: AsyncConnection,
    *,
    run_id: UUID,
    attribute_code: str,
    modality: str,
    top1_acc: float,
    top3_acc: float,
    by_hardness: dict[str, Any],
    engine_version: str,
) -> None:
    """Insert one attribute's top-1/top-3 accuracy (+ optional by-hardness breakdown) for a run."""
    await conn.execute(
        text(
            "INSERT INTO eval_results (run_id, attribute_code, modality, top1_acc, top3_acc, "
            "by_hardness, engine_version) VALUES (:run_id, :attr, :modality, :top1, :top3, "
            "CAST(:by_hardness AS jsonb), :ev)"
        ),
        {
            "run_id": run_id,
            "attr": attribute_code,
            "modality": modality,
            "top1": top1_acc,
            "top3": top3_acc,
            "by_hardness": json.dumps(by_hardness),
            "ev": engine_version,
        },
    )


@dataclass(frozen=True)
class EvalResultRow:
    """One attribute's benchmark accuracy for the latest eval run — the public trust read (M2)."""

    attribute_code: str
    modality: str
    top1: float
    top3: float
    engine_version: str


async def latest_eval_results(conn: AsyncConnection) -> list[EvalResultRow]:
    """Every attribute's top-1/top-3 for the most recent `eval` run — the public accuracy view.

    Reads the non-tenant `eval_results` (no RLS; app-role SELECT granted in 0010). The newest eval
    run is resolved via `latest_eval_run_id()` (SECURITY DEFINER), so `runs` RLS is never in the
    path and this works on a no-user public connection. Empty when no benchmark has run yet.
    """
    result = await conn.execute(
        text(
            "SELECT attribute_code, modality, top1_acc, top3_acc, engine_version "
            "FROM eval_results WHERE run_id = latest_eval_run_id() ORDER BY attribute_code"
        )
    )
    return [
        EvalResultRow(
            attribute_code=row[0],
            modality=row[1],
            top1=float(row[2]) if row[2] is not None else 0.0,
            top3=float(row[3]) if row[3] is not None else 0.0,
            engine_version=row[4],
        )
        for row in result
    ]


async def latest_eval_engine_version(conn: AsyncConnection) -> str | None:
    """The latest eval run's *bare* engine_version (the calibration map's key), or None if no run.

    Resolved via the `latest_eval_engine_version()` SECURITY DEFINER helper (bypasses `runs` RLS for
    the public reader). `runs.engine_version` is stored bare (no judge suffix), so it is the key the
    calibration curve needs — and the same run whose accuracy rows `/results` shows, so the trust
    view's two halves describe one engine. NOT `eval_results.engine_version`, which may carry a
    `+match_judge_v1@judge` scoring suffix the calibration map is never keyed on.
    """
    result = await conn.execute(text("SELECT latest_eval_engine_version()"))
    engine_version: str | None = result.scalar_one()
    return engine_version


async def latest_eval_top1(conn: AsyncConnection) -> dict[str, float]:
    """Per-attribute top-1 accuracy for the most recent `eval` run (the CI floor gate's input).

    Privileged read (no app grant on `eval_results`). Empty when no benchmark has run yet.
    """
    result = await conn.execute(
        text(
            "SELECT er.attribute_code, er.top1_acc FROM eval_results er "
            "WHERE er.run_id = ("
            "  SELECT id FROM runs WHERE type = 'eval' ORDER BY created_at DESC LIMIT 1"
            ")"
        )
    )
    return {row[0]: float(row[1]) for row in result}
