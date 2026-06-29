"""Unit (M1.8b): occupation self-consistency via the semantic judge + the string fallback."""

from app.domain.output_schema import AttributeGuess, Candidate, Confidence, FreeTextValue
from app.services.occupation import (
    GatewayOccupationJudge,
    StringMatchJudge,
    aggregate_occupation,
)

_ROLES = {"swe", "software engineer"}


def _occ_run(text: str, conf: float = 0.8) -> AttributeGuess:
    return AttributeGuess(
        attribute="occupation",
        modality="text",
        status="inferred",
        candidates=[
            Candidate(
                rank=1,
                value=FreeTextValue(text=text),
                confidence=Confidence(raw=conf, source="self_reported", self_reported=conf),
            )
        ],
        reasoning="r",
    )


class _FakeJudge:
    """Treats SWE ≈ software engineer as equivalent; everything else is string equality."""

    async def equivalent(self, a: str, b: str) -> bool:
        if a.lower() in _ROLES and b.lower() in _ROLES:
            return True
        return a.lower() == b.lower()


class _BrokenGateway:
    async def judge_same(self, a: str, b: str) -> bool:
        raise RuntimeError("judge down")


class _OkGateway:
    def __init__(self, result: bool) -> None:
        self._result = result

    async def judge_same(self, a: str, b: str) -> bool:
        return self._result


async def test_judge_clusters_synonyms() -> None:
    runs = [_occ_run("SWE"), _occ_run("software engineer"), _occ_run("teacher")]
    result = await aggregate_occupation(runs, _FakeJudge(), n_runs=3)
    assert result.status == "inferred"
    assert result.candidates[0].confidence.raw == 2 / 3  # the two SWE labels cluster


async def test_string_judge_keeps_synonyms_distinct() -> None:
    runs = [_occ_run("SWE"), _occ_run("software engineer"), _occ_run("teacher")]
    result = await aggregate_occupation(runs, StringMatchJudge(), n_runs=3)
    assert result.status == "abstained"  # three distinct strings → no plurality


async def test_string_judge_normalizes_case_and_whitespace() -> None:
    runs = [_occ_run("Software Engineer"), _occ_run("software  engineer"), _occ_run("nurse")]
    result = await aggregate_occupation(runs, StringMatchJudge(), n_runs=3)
    assert result.candidates[0].confidence.raw == 2 / 3


async def test_gateway_judge_delegates_to_the_slot() -> None:
    assert await GatewayOccupationJudge(_OkGateway(True)).equivalent("a", "b") is True
    assert await GatewayOccupationJudge(_OkGateway(False)).equivalent("a", "b") is False


async def test_gateway_judge_falls_back_to_string_on_error() -> None:
    judge = GatewayOccupationJudge(_BrokenGateway())  # §8: degrade, never fail the attack
    assert await judge.equivalent("nurse", "nurse") is True
    assert await judge.equivalent("swe", "software engineer") is False
