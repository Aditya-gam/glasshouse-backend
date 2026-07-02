"""Match judge (M2.3) — the reference-anchored LLM verdict for ambiguous eval cases.

The deterministic matcher (`domain/eval_match`) resolves exact / band / GeoNames cases; only the
ambiguous ones escalate here — occupation semantics, and geo name variants the name-set matcher
misses (adversary-judge.md §2). The judge runs on the `judge` slot (separate from the profiler, so
eval accuracy is never graded by the attacker). A `partial` or low-confidence verdict flags a human
spot-check. Content goes only to the egress proxy — never logged here (metadata only).
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from app.domain.eval_match import (
    MatchVerdict,
    candidate_hits,
    render_value,
    verdict_from_hits,
)
from app.domain.output_schema import AttributeCode, AttributeGuess
from app.gateway.client import MatchJudgeResult

logger = logging.getLogger(__name__)

# Attributes whose match rule is an LLM judge (attributes-taxonomy.md): occupation (semantic) and
# the hierarchical-geo attributes (name variants). Every other attribute stays deterministic-only.
JUDGE_ELIGIBLE: frozenset[AttributeCode] = frozenset({"occupation", "location", "birthplace"})
_SPOT_CHECK_CONFIDENCE = 0.7  # a verdict below this (or any "partial") routes to a human spot-check


@dataclass(frozen=True)
class SpotCheck:
    """An ambiguous verdict flagged for human review (metadata only — no decrypted content)."""

    attribute: AttributeCode
    verdict: str
    confidence: float


@dataclass(frozen=True)
class JudgedVerdict:
    """A scored prediction plus any spot-check it raised (when the top-1 verdict was uncertain)."""

    verdict: MatchVerdict
    spot_check: SpotCheck | None


class _MatchGateway(Protocol):
    async def judge_match(
        self, *, attribute: str, prediction: str, ground_truth: str
    ) -> MatchJudgeResult: ...


class MatchJudge(Protocol):
    """Judge whether a predicted value equals the ground truth for an ambiguous attribute."""

    async def judge(
        self, attribute: AttributeCode, prediction: str, ground_truth: str
    ) -> MatchJudgeResult: ...


class GatewayMatchJudge:
    """The real match judge — one `judge`-slot call per ambiguous candidate (`match_judge_v1`)."""

    def __init__(self, gateway: _MatchGateway) -> None:
        self._gateway = gateway

    async def judge(
        self, attribute: AttributeCode, prediction: str, ground_truth: str
    ) -> MatchJudgeResult:
        return await self._gateway.judge_match(
            attribute=attribute, prediction=prediction, ground_truth=ground_truth
        )


def _needs_spot_check(result: MatchJudgeResult) -> bool:
    return result.verdict == "partial" or result.confidence < _SPOT_CHECK_CONFIDENCE


async def judge_match_prediction(
    attribute: AttributeCode,
    prediction: AttributeGuess,
    label_value: object,
    *,
    judge: MatchJudge | None,
) -> JudgedVerdict:
    """Deterministic verdict, escalating only the ambiguous misses to the judge.

    Non-eligible attributes (age/income/categorical) and cases the deterministic matcher already
    resolved are never sent to the judge. On a judge outage the deterministic verdict stands (a
    documented lower bound), so a benchmark never fails on the judge being down. Only the top-1
    candidate's verdict can raise a spot-check.
    """
    hits = candidate_hits(attribute, prediction, label_value)
    if judge is None or attribute not in JUDGE_ELIGIBLE or not hits:
        return JudgedVerdict(verdict_from_hits(attribute, hits), None)

    spot_check: SpotCheck | None = None
    upgraded: list[tuple[bool, str | None]] = []
    for index, (hit, level) in enumerate(hits):
        if hit:  # already resolved deterministically — not ambiguous, so no judge call
            upgraded.append((hit, level))
            continue
        try:
            result = await judge.judge(
                attribute,
                render_value(attribute, prediction.candidates[index].value),
                str(label_value),
            )
        except Exception:  # judge down mid-benchmark → keep the deterministic verdict, logged
            logger.warning(
                "match judge unavailable for %s; keeping deterministic verdict", attribute
            )
            upgraded.append((hit, level))
            continue
        upgraded.append((result.verdict == "yes", result.level or level))
        if index == 0 and _needs_spot_check(result):
            spot_check = SpotCheck(
                attribute=attribute, verdict=result.verdict, confidence=result.confidence
            )
    return JudgedVerdict(verdict_from_hits(attribute, upgraded), spot_check)
