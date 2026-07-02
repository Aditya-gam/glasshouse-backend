"""Data access for `eval_results` — per-attribute benchmark accuracy for one eval run (Job 1).

Written on a **privileged** connection by the eval service (M2.2). Feeds the CI floor gate and the
public accuracy/trust number; no data subject, so no encryption.
"""

import json
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
