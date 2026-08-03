"""Eval endpoints — the engine's public credibility numbers (Job 1, M2).

Public (no auth): the cited accuracy/calibration is a selling point, and the data is non-personal.
The eval *trigger* is `POST /v1/runs {type:"eval"}` (privileged), handled by the runs router.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.api.deps import get_public_session
from app.api.v1.schemas import BenchmarkRead, BenchRow, EvalResultRead
from app.repositories.calibration import list_calibration_points
from app.repositories.eval_results import latest_eval_engine_version, latest_eval_results

router = APIRouter(prefix="/v1/eval", tags=["eval"])


@router.get("/results")
async def eval_results(
    conn: Annotated[AsyncConnection, Depends(get_public_session)],
) -> list[EvalResultRead]:
    """Top-1/top-3 per attribute + modality + engine_version — the accuracy-trust view.

    The latest benchmark run's per-attribute accuracy; empty until an eval has run.
    """
    rows = await latest_eval_results(conn)
    return [
        EvalResultRead(
            attribute=row.attribute_code,
            modality=row.modality,
            top1=row.top1,
            top3=row.top3,
            engine_version=row.engine_version,
        )
        for row in rows
    ]


@router.get("/calibration")
async def eval_calibration(
    conn: Annotated[AsyncConnection, Depends(get_public_session)],
) -> BenchmarkRead:
    """The reliability curve (calibrated reliability + benchmark rows) for the trust display.

    `rows` is the latest benchmark's per-attribute accuracy; `calibration` is the pooled
    `[predicted, empirical]` curve for that same run's engine (so both halves describe one engine).
    Both empty until an eval has run.
    """
    rows = await latest_eval_results(conn)
    engine_version = await latest_eval_engine_version(conn)
    curve = [] if engine_version is None else await list_calibration_points(conn, engine_version)
    return BenchmarkRead(
        rows=[BenchRow(label=row.attribute_code, top1=row.top1, top3=row.top3) for row in rows],
        calibration=curve,
    )
