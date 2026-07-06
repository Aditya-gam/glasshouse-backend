"""Unit (M3.1): the independent adversary — blind re-attack, scoping, value-recovery."""

from app.domain.output_schema import (
    AttributeGuess,
    Candidate,
    Confidence,
    GeoHierValue,
    RawAttributeGuess,
    RawCandidate,
)
from app.services.adversary import adversary_attack, value_recovered
from app.services.geocoding import GeoResolution
from app.services.occupation import StringMatchJudge


class _FakeGeocoder:
    async def resolve(self, place: str) -> GeoResolution | None:
        return None


class _FakeAdversary:
    """A held-out adversary: recovers location, abstains on occupation; records what it saw."""

    def __init__(self) -> None:
        self.seen_content: list[str] = []

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        self.seen_content.append(content)
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.9)],
            ),
            RawAttributeGuess(attribute="occupation", status="abstained", candidates=[]),
        ]


def _seattle_guess() -> AttributeGuess:
    return AttributeGuess(
        attribute="location",
        modality="text",
        status="inferred",
        candidates=[
            Candidate(
                rank=1,
                value=GeoHierValue(country="United States", city="Seattle", precision_level="city"),
                confidence=Confidence(raw=1.0, source="self_consistency"),
            )
        ],
    )


def test_value_recovered_true_when_truth_in_top3() -> None:
    guess = _seattle_guess()

    assert value_recovered("location", guess, "Seattle, USA") is True  # name-set + alias match
    assert value_recovered("location", guess, "Boston, USA") is False


async def test_adversary_recovers_the_true_location() -> None:
    adversary = _FakeAdversary()

    finding = await adversary_attack(
        adversary,
        [("itm1", "A PST morning walk in Seattle.")],
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
        judge=StringMatchJudge(),
        n_runs=2,
        temperature=0.0,
    )

    assert finding.attribute == "location"
    assert finding.guess.status == "inferred"
    assert finding.value_recovered is True  # the rewrite did NOT hide the location


async def test_adversary_is_blind_to_everything_but_the_content() -> None:
    adversary = _FakeAdversary()

    await adversary_attack(
        adversary,
        [("itm1", "the edited text only")],
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
        n_runs=1,
    )

    # the adversary only ever sees the datamarked content — never an original/diff/reasoning field.
    assert all("the edited text only" in seen for seen in adversary.seen_content)
    assert all("original" not in seen.lower() for seen in adversary.seen_content)


class _WrongAdversary:
    """A fooled adversary: it still confidently infers a location, but the WRONG one."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Boston, MA", self_confidence=0.9)],
            )
        ]


async def test_inferred_but_wrong_value_is_not_recovered() -> None:
    # the defense-won signal: the adversary still guesses, but guesses wrong → not recovered.
    finding = await adversary_attack(
        _WrongAdversary(),
        [("itm1", "the edited text")],
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
        n_runs=2,
        temperature=0.0,
    )

    assert finding.guess.status == "inferred"  # the adversary was NOT silenced...
    assert finding.value_recovered is False  # ...but it no longer recovers the true value


async def test_adversary_scoped_to_target_abstains_when_not_inferred() -> None:
    # the adversary abstains on occupation → not recovered (what a successful defense looks like).
    finding = await adversary_attack(
        _FakeAdversary(),
        [("itm1", "some text")],
        target_attribute="occupation",
        true_value="software engineer",
        geocoder=_FakeGeocoder(),
        n_runs=2,
    )

    assert finding.guess.status == "abstained" and finding.value_recovered is False
