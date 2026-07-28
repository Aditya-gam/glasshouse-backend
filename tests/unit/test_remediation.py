"""Unit (M3.7): plan_remediation — ablation → rewrite → proven before/after, with honest failure.

Injects fakes for every model slot (adversary, feedback adversary, anonymizer, utility judge,
embedder). The scenario: one load-bearing item pins "Seattle, WA"; generalizing it breaks the
held-out adversary's recovery. Covers the proven flip, the un-localizable no-op, and the honest
non-success (a rewrite that does NOT break recovery is still recorded — never false safety).
"""

from app.domain.output_schema import (
    FreeTextValue,
    GeoHierValue,
    NumericValue,
    RawAttributeGuess,
    RawCandidate,
)
from app.gateway.client import AnonymizerEdit, UtilityGrade, UtilityVerdict
from app.gateway.prompts import ADVERSARY_VERSION
from app.services.adversary import Adversary
from app.services.anonymize import Anonymizer
from app.services.geocoding import GeoResolution
from app.services.remediation import RemediationOutcome, _defended_value, plan_remediation

_PIN = "Seattle"


class _FakeGeocoder:
    async def resolve(self, place: str) -> GeoResolution | None:
        return None


class _FakeEmbedder:
    """A high fixed similarity so assess_utility's pre-filter never rejects (utility via judge)."""

    @property
    def dimension(self) -> int:
        return 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 1.0, 1.0] for _ in texts]


def _recovers(content: str) -> list[RawAttributeGuess]:
    """The adversary/feedback model: recovers 'Seattle, WA' iff the pin survives in the content."""
    if _PIN in content:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.8)],
            )
        ]
    return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]


class _PinAdversary:
    """Held-out adversary + feedback: recovery keyed on the surviving pin (blind to the edit)."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return _recovers(content)


async def _feedback_profile_fn(content: str, temperature: float) -> list[RawAttributeGuess]:
    return _recovers(content)


class _GeneralizingAnonymizer:
    """Generalizes the pin away (a truthful rewrite)."""

    async def anonymize(
        self, *, text: str, spans: list[str], attribute: str, feedback: str | None = None
    ) -> AnonymizerEdit:
        return AnonymizerEdit(
            reasoning="generalize the cue",
            operation="generalize",
            edited_text=text.replace(_PIN, "a nearby city"),
            note="generalized the location",
        )


class _StubbornAnonymizer:
    """Never removes the cue — exercises the honest non-success path (no false safety)."""

    async def anonymize(
        self, *, text: str, spans: list[str], attribute: str, feedback: str | None = None
    ) -> AnonymizerEdit:
        return AnonymizerEdit(
            reasoning="tried", operation="generalize", edited_text=text, note="unchanged"
        )


class _FakeUtilityJudge:
    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: str
    ) -> UtilityVerdict:
        grade: UtilityGrade = "mostly" if criterion == "meaning" else "fully"
        return UtilityVerdict(reasoning="", grade=grade, confidence=0.9)


_CONTENT = [("a", "I live near Seattle"), ("b", "great coffee downtown")]


async def _plan(adversary: Adversary, anonymizer: Anonymizer) -> RemediationOutcome | None:
    return await plan_remediation(
        content=_CONTENT,
        target_attribute="location",
        true_value="Seattle, WA",
        adversary=adversary,
        feedback_profile_fn=_feedback_profile_fn,
        anonymizer=anonymizer,
        utility_judge=_FakeUtilityJudge(),
        embedder=_FakeEmbedder(),
        geocoder=_FakeGeocoder(),
        judge=None,
        floor_margin=0.1,
    )


async def test_proven_rewrite_flips_recovery_and_is_recorded() -> None:
    outcome = await _plan(_PinAdversary(), _GeneralizingAnonymizer())

    assert outcome is not None
    assert outcome.action == "rewrite"
    assert outcome.primary_edited_text == "I live near a nearby city"  # only the pin item changed
    assert outcome.delta.value_recovery_flip is True  # recovered before, not after — the headline
    assert outcome.delta.significant is True  # the confidence drop clears the noise floor
    assert outcome.recovered_before is True and outcome.recovered_after is False
    assert outcome.span_changes == [
        {"item_id": "a", "op": "generalize", "replacement": "I live near a nearby city"}
    ]
    assert outcome.utility.meaning == "mostly"  # utility judged on the surviving meaning
    assert outcome.evaluator_engine_version == ADVERSARY_VERSION


async def test_unlocalizable_leak_yields_no_option() -> None:
    # an adversary that never recovers → nothing to localize → None (read layer reports honestly).
    class _BlindAdversary:
        async def adversary_profile_all(
            self, *, content: str, temperature: float = 0.0
        ) -> list[RawAttributeGuess]:
            return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]

    outcome = await _plan(_BlindAdversary(), _GeneralizingAnonymizer())

    assert outcome is None


async def test_rewrite_that_does_not_break_is_recorded_without_false_safety() -> None:
    outcome = await _plan(_PinAdversary(), _StubbornAnonymizer())

    assert outcome is not None  # the honest non-success is still persisted
    assert outcome.recovered_after is True  # the adversary still recovers → no safety claimed
    assert outcome.delta.value_recovery_flip is False
    assert outcome.delta.significant is False  # no real drop


def test_defended_value_uses_the_income_bracket_not_the_estimate() -> None:
    # the income match rule compares to a bracket WORD; rendering the estimate ("95000") would never
    # match, making income look permanently un-recoverable. The bracket must be the compared value.
    income = NumericValue(estimate=95000, bracket="high")
    assert _defended_value("income", income) == "high"

    income_no_bracket = NumericValue(estimate=25000)  # falls back to income_bracket(estimate)
    assert _defended_value("income", income_no_bracket) == "low"

    # other attributes render to their label-compatible string (unchanged behavior).
    assert _defended_value("occupation", FreeTextValue(text="nurse")) == "nurse"
    geo = GeoHierValue(city="Seattle", country="USA", precision_level="city")
    assert _defended_value("location", geo) == "Seattle, USA"
