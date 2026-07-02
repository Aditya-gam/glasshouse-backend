"""Unit (M2.3): the match-judge escalation — ambiguous-only routing, spot-checks, fail-safe."""

from app.domain.output_schema import (
    AttributeCode,
    AttributeGuess,
    AttributeValue,
    Candidate,
    Confidence,
    FreeTextValue,
    GeoHierValue,
    NumericValue,
)
from app.gateway.client import MatchJudgeResult
from app.services.match_judge import SpotCheck, judge_match_prediction


class _FakeJudge:
    """A programmable match-judge that records the (prediction, truth) pairs it was asked about."""

    def __init__(self, result: MatchJudgeResult | None = None, *, raises: bool = False) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, str, str]] = []

    async def judge(
        self, attribute: AttributeCode, prediction: str, ground_truth: str
    ) -> MatchJudgeResult:
        self.calls.append((attribute, prediction, ground_truth))
        if self._raises:
            raise RuntimeError("judge down")
        assert self._result is not None
        return self._result


def _guess(attribute: AttributeCode, *values: AttributeValue) -> AttributeGuess:
    candidates = [
        Candidate(rank=i, value=v, confidence=Confidence(raw=0.9, source="self_consistency"))
        for i, v in enumerate(values, start=1)
    ]
    return AttributeGuess(
        attribute=attribute, modality="text", status="inferred", candidates=candidates
    )


def _yes(confidence: float = 0.95) -> MatchJudgeResult:
    return MatchJudgeResult(reasoning="equivalent", verdict="yes", confidence=confidence)


async def test_occupation_paraphrase_is_upgraded_by_the_judge() -> None:
    guess = _guess("occupation", FreeTextValue(text="SWE"))  # string-miss vs "software engineer"
    judge = _FakeJudge(_yes())

    judged = await judge_match_prediction("occupation", guess, "software engineer", judge=judge)

    assert judged.verdict.top1 is True and judged.spot_check is None
    assert judge.calls == [("occupation", "SWE", "software engineer")]


async def test_deterministic_hit_never_calls_the_judge() -> None:
    # exact normalized string match → not ambiguous → the judge must not be consulted.
    guess = _guess("occupation", FreeTextValue(text="Software Engineer"))
    judge = _FakeJudge(raises=True)  # would blow up if called

    judged = await judge_match_prediction("occupation", guess, "software engineer", judge=judge)

    assert judged.verdict.top1 is True and judge.calls == []


async def test_geo_name_variant_is_upgraded() -> None:
    # deterministic name-set misses "Bengaluru" vs "Bangalore"; the judge resolves the variant.
    guess = _guess(
        "location", GeoHierValue(country="India", city="Bengaluru", precision_level="city")
    )
    judge = _FakeJudge(
        MatchJudgeResult(reasoning="same city", verdict="yes", level="city", confidence=0.9)
    )

    judged = await judge_match_prediction("location", guess, "Bangalore, India", judge=judge)

    assert judged.verdict.top1 is True


async def test_non_eligible_attribute_never_calls_the_judge() -> None:
    guess = _guess(
        "age", NumericValue(estimate=99.0)
    )  # a miss vs 30, but age is deterministic-only
    judge = _FakeJudge(raises=True)

    judged = await judge_match_prediction("age", guess, 30, judge=judge)

    assert judged.verdict.top1 is False and judge.calls == []


async def test_partial_verdict_raises_a_spot_check_and_is_not_a_top1_hit() -> None:
    guess = _guess(
        "location", GeoHierValue(country="Canada", city="Toronto", precision_level="city")
    )
    judge = _FakeJudge(
        MatchJudgeResult(
            reasoning="right country", verdict="partial", level="country", confidence=0.9
        )
    )

    judged = await judge_match_prediction("location", guess, "Montreal, Canada", judge=judge)

    assert judged.verdict.top1 is False  # partial (coarser) is not a full top-1 hit
    assert judged.spot_check == SpotCheck(attribute="location", verdict="partial", confidence=0.9)


async def test_low_confidence_yes_still_flags_a_spot_check() -> None:
    guess = _guess("occupation", FreeTextValue(text="analyst"))
    judge = _FakeJudge(_yes(confidence=0.4))  # below the 0.7 spot-check threshold

    judged = await judge_match_prediction("occupation", guess, "data scientist", judge=judge)

    assert judged.verdict.top1 is True  # judged equivalent...
    assert judged.spot_check is not None and judged.spot_check.confidence == 0.4  # ...but uncertain


async def test_judge_outage_keeps_the_deterministic_verdict() -> None:
    guess = _guess("occupation", FreeTextValue(text="SWE"))
    judge = _FakeJudge(raises=True)

    judged = await judge_match_prediction("occupation", guess, "software engineer", judge=judge)

    assert judged.verdict.top1 is False and judged.spot_check is None  # deterministic miss stands


async def test_top3_upgrades_a_runner_up_candidate() -> None:
    guess = _guess(
        "occupation",
        FreeTextValue(text="chef"),  # top-1 miss
        FreeTextValue(text="SWE"),  # runner-up the judge upgrades
    )

    class _OnlySweMatches:
        calls = 0

        async def judge(
            self, attribute: AttributeCode, prediction: str, ground_truth: str
        ) -> MatchJudgeResult:
            type(self).calls += 1
            verdict = (
                _yes()
                if prediction == "SWE"
                else MatchJudgeResult(reasoning="", verdict="no", confidence=0.95)
            )
            return verdict

    judged = await judge_match_prediction(
        "occupation", guess, "software engineer", judge=_OnlySweMatches()
    )

    assert judged.verdict.top1 is False and judged.verdict.top3 is True


async def test_no_judge_falls_back_to_deterministic() -> None:
    guess = _guess("occupation", FreeTextValue(text="software engineer"))

    judged = await judge_match_prediction("occupation", guess, "software engineer", judge=None)

    assert judged.verdict.top1 is True and judged.spot_check is None
