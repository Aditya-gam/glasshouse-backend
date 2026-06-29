"""Occupation self-consistency — semantic clustering via an LLM judge (confidence doc §3, §8).

Occupation is the one attribute whose match rule is an **LLM semantic judge** ("SWE" ≈ "software
engineer"), so its clustering is IO and lives here at the service layer; it reuses the pure
`build_consensus` from the domain. `StringMatchJudge` is the deterministic default and the §8
degraded fallback. The judge runs on the separate `judge` slot, so a guess is never de-duplicated by
the model that produced it. Occupation labels are not Art. 9 and are sent only to the egress proxy
(never logged here).
"""

import logging
from collections.abc import Sequence
from typing import Protocol

from app.domain.consistency import build_consensus
from app.domain.output_schema import AttributeGuess, FreeTextValue

logger = logging.getLogger(__name__)


class OccupationJudge(Protocol):
    """Decide whether two free-text occupation labels denote the same profession."""

    async def equivalent(self, a: str, b: str) -> bool: ...


class StringMatchJudge:
    """Deterministic fallback (§8): labels match iff their normalized strings are equal."""

    async def equivalent(self, a: str, b: str) -> bool:
        return " ".join(a.lower().split()) == " ".join(b.lower().split())


class _JudgeGateway(Protocol):
    async def judge_same(self, a: str, b: str) -> bool: ...


class GatewayOccupationJudge:
    """The real judge (gateway `judge` slot); degrades to string match if it is unavailable (§8)."""

    def __init__(self, gateway: _JudgeGateway, fallback: OccupationJudge | None = None) -> None:
        self._gateway = gateway
        self._fallback = fallback or StringMatchJudge()

    async def equivalent(self, a: str, b: str) -> bool:
        try:
            return await self._gateway.judge_same(a, b)
        except Exception:  # judge down mid-run → degrade (handled, logged), never fail the attack
            logger.warning("occupation judge unavailable; falling back to string match")
            return await self._fallback.equivalent(a, b)


def _label(guess: AttributeGuess) -> str:
    value = guess.candidates[0].value
    if not isinstance(
        value, FreeTextValue
    ):  # the validator guarantees freetext; guard for the type
        raise TypeError("expected a freetext_semantic value")
    return value.text


async def aggregate_occupation(
    guesses: Sequence[AttributeGuess], judge: OccupationJudge, *, n_runs: int
) -> AttributeGuess:
    """Cluster the N occupation runs by semantic equivalence (judge), then build the consensus."""
    picks = [g for g in guesses if g.status == "inferred" and g.candidates]
    clusters: list[list[AttributeGuess]] = []
    for guess in picks:
        label = _label(guess)
        for cluster in clusters:
            if await judge.equivalent(_label(cluster[0]), label):
                cluster.append(guess)
                break
        else:
            clusters.append([guess])
    return build_consensus("occupation", clusters, n_runs=n_runs)
