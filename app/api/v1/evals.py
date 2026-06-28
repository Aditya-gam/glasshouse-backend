"""Eval endpoints — the engine's public credibility numbers (Job 1). Stubs until M2."""

from fastapi import APIRouter

from app.api.errors import NotImplementedYet
from app.api.v1.schemas import BenchmarkRead, EvalResultRead

# Read endpoints are public (the cited number is a selling point); the eval *trigger* is
# POST /v1/runs {type:"eval"} (privileged), handled by the runs router.
router = APIRouter(prefix="/v1/eval", tags=["eval"])

_NOT_YET = "benchmark + calibration land at M2"


@router.get("/results")
async def eval_results() -> list[EvalResultRead]:
    """Top-1/top-3 per attribute + modality + engine_version — the accuracy-trust view."""
    raise NotImplementedYet(_NOT_YET)


@router.get("/calibration")
async def eval_calibration() -> BenchmarkRead:
    """The reliability curve (calibrated reliability + the noise model)."""
    raise NotImplementedYet(_NOT_YET)
