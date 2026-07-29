"""Unit (M3.7b): plan_frontier — the proven advise-only frontier (minimal/stronger/remove/decoy).

Injects fakes for every model slot. The scenario: one load-bearing item pins "Seattle, WA".
Generalizing or removing it breaks the held-out adversary's recovery; the decoy steers the adversary
to a plausible false city. Covers the frontier shape, per-option honesty (a rewrite that fails to
break is still recorded), the un-localizable empty frontier, and the income bracket derivation.
"""

from app.domain.output_schema import (
    FreeTextValue,
    GeoHierValue,
    NumericValue,
    RawAttributeGuess,
    RawCandidate,
)
from app.gateway.client import AnonymizerEdit, DecoyEdit, UtilityGrade, UtilityVerdict
from app.gateway.prompts import ADVERSARY_VERSION
from app.services.adversary import Adversary
from app.services.anonymize import Anonymizer
from app.services.decoy import DecoyEditor
from app.services.geocoding import GeoResolution
from app.services.inference import ProfileFn
from app.services.remediation import RemediationOutcome, _defended_value, plan_frontier

_PIN = "Seattle"


class _FakeGeocoder:
    async def resolve(self, place: str) -> GeoResolution | None:
        return None


class _FakeEmbedder:
    """A high, fixed similarity so assess_utility's pre-filter never rejects (utility via judge)."""

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
    """Held-out adversary: recovery keyed on the surviving pin (blind to the edit)."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return _recovers(content)


async def _feedback_profile_fn(content: str, temperature: float) -> list[RawAttributeGuess]:
    return _recovers(content)


class _GeneralizingAnonymizer:
    """Generalizes the pin away (a truthful rewrite), regardless of strength."""

    async def anonymize(
        self,
        *,
        text: str,
        spans: list[str],
        attribute: str,
        feedback: str | None = None,
        strength: str = "minimal",
    ) -> AnonymizerEdit:
        return AnonymizerEdit(
            reasoning="generalize",
            operation="generalize",
            edited_text=text.replace(_PIN, "a nearby city"),
            note="generalized the location",
        )


class _StubbornAnonymizer:
    """Never removes the cue — exercises the per-option honest non-success (no false safety)."""

    async def anonymize(
        self,
        *,
        text: str,
        spans: list[str],
        attribute: str,
        feedback: str | None = None,
        strength: str = "minimal",
    ) -> AnonymizerEdit:
        return AnonymizerEdit(
            reasoning="tried", operation="generalize", edited_text=text, note="unchanged"
        )


class _FakeDecoyEditor:
    """Injects a plausible false city (steers the adversary off the true value)."""

    async def decoy(
        self, *, text: str, spans: list[str], attribute: str, decoy_value: str | None = None
    ) -> DecoyEdit:
        return DecoyEdit(
            reasoning="inject a false cue",
            edited_text=text.replace(_PIN, "Portland"),
            decoy_value="Portland, OR",
            note="implies a different city",
        )


class _FakeUtilityJudge:
    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: str
    ) -> UtilityVerdict:
        grade: UtilityGrade = "mostly" if criterion == "meaning" else "fully"
        return UtilityVerdict(reasoning="", grade=grade, confidence=0.9)


_CONTENT = [("a", "I live near Seattle"), ("b", "great coffee downtown")]


async def _frontier(
    adversary: Adversary,
    anonymizer: Anonymizer,
    *,
    decoy_editor: DecoyEditor | None = None,
    feedback_profile_fn: ProfileFn = _feedback_profile_fn,
) -> list[RemediationOutcome]:
    return await plan_frontier(
        content=_CONTENT,
        target_attribute="location",
        true_value="Seattle, WA",
        adversary=adversary,
        feedback_profile_fn=feedback_profile_fn,
        anonymizer=anonymizer,
        utility_judge=_FakeUtilityJudge(),
        embedder=_FakeEmbedder(),
        geocoder=_FakeGeocoder(),
        judge=None,
        floor_margin=0.1,
        decoy_editor=decoy_editor,
    )


async def test_frontier_has_minimal_stronger_remove_all_proven() -> None:
    outcomes = await _frontier(_PinAdversary(), _GeneralizingAnonymizer())

    assert [o.option_key for o in outcomes] == ["minimal", "stronger", "remove"]
    assert all(o.delta.value_recovery_flip is True for o in outcomes)  # each breaks recovery
    assert all(o.delta.significant is True for o in outcomes)  # each clears the noise floor
    minimal = outcomes[0]
    assert minimal.action == "rewrite"
    assert minimal.primary_edited_text == "I live near a nearby city"  # only the pin item changed
    remove = outcomes[2]
    assert remove.action == "remove" and remove.primary_edited_text is None
    assert remove.utility.meaning == "lost"  # removal keeps no utility


async def test_frontier_adds_decoy_when_editor_given() -> None:
    outcomes = await _frontier(
        _PinAdversary(), _GeneralizingAnonymizer(), decoy_editor=_FakeDecoyEditor()
    )

    assert [o.option_key for o in outcomes] == ["minimal", "stronger", "remove", "decoy"]
    decoy = outcomes[3]
    assert decoy.is_decoy is True and decoy.action == "decoy"
    assert decoy.misled_value == "Portland, OR"  # the plausible false value now implied
    assert decoy.delta.value_recovery_flip is True  # the adversary no longer recovers the truth


async def test_unlocalizable_leak_yields_empty_frontier() -> None:
    class _BlindAdversary:
        async def adversary_profile_all(
            self, *, content: str, temperature: float = 0.0
        ) -> list[RawAttributeGuess]:
            return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]

    outcomes = await _frontier(_BlindAdversary(), _GeneralizingAnonymizer())

    assert outcomes == []  # nothing to localize → read layer reports cant_break


async def test_rewrite_that_fails_is_recorded_but_remove_still_breaks() -> None:
    # a stubborn rewrite doesn't break recovery — recorded honestly (no false safety) — while the
    # remove option still flips. Per-option honesty across the frontier.
    outcomes = await _frontier(_PinAdversary(), _StubbornAnonymizer())

    by_key = {o.option_key: o for o in outcomes}
    assert by_key["minimal"].recovered_after is True  # the pin survived → still exposed
    assert by_key["minimal"].delta.value_recovery_flip is False
    assert by_key["remove"].delta.value_recovery_flip is True  # removal always breaks it


def test_defended_value_uses_the_income_bracket_not_the_estimate() -> None:
    # the income match rule compares to a bracket WORD; rendering the estimate ("95000") would never
    # match, making income look permanently un-recoverable. The bracket must be the compared value.
    assert _defended_value("income", NumericValue(estimate=95000, bracket="high")) == "high"
    assert _defended_value("income", NumericValue(estimate=25000)) == "low"  # bracket fallback
    assert _defended_value("occupation", FreeTextValue(text="nurse")) == "nurse"
    geo = GeoHierValue(city="Seattle", country="USA", precision_level="city")
    assert _defended_value("location", geo) == "Seattle, USA"


def test_evaluator_engine_pins_the_adversary() -> None:
    assert ADVERSARY_VERSION.endswith("@adversary")
