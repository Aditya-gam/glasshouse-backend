"""Unit (M3.2): attribution by ablation — greedy minimal set, redundancy, pruning, cap."""

from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.services.ablation import AblationResult, find_minimal_set
from app.services.geocoding import GeoResolution


class _FakeGeocoder:
    async def resolve(self, place: str) -> GeoResolution | None:
        return None


class _PinAdversary:
    """Recovers "Seattle, WA" iff a pin phrase survives in the content; confidence rises with the
    number of surviving pins, and a "buzzwords" noise item boosts it without being a cause."""

    _PINS = ("Seattle", "Emerald City")  # each phrase independently pins the city

    def __init__(self) -> None:
        self.probes = 0

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        self.probes += 1
        pins = sum(1 for pin in self._PINS if pin in content)
        if pins == 0:
            return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]
        boost = 0.5 if "buzzwords" in content else 0.0
        confidence = min(1.0, 0.3 * pins + boost)
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=confidence)],
            )
        ]


async def _ablate(adversary: _PinAdversary, content: list[tuple[str, str]]) -> AblationResult:
    return await find_minimal_set(
        adversary,
        content,
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
    )


async def test_single_cause_is_the_only_item_in_the_minimal_set() -> None:
    content = [("a", "I live in Seattle"), ("b", "good coffee"), ("c", "so rainy")]

    result = await _ablate(_PinAdversary(), content)

    assert result.minimal_set == ["a"] and result.sufficient is True
    assert result.marginal_effect["a"] > 0  # the cause carries the drop
    assert result.marginal_effect["b"] == 0.0 and result.marginal_effect["c"] == 0.0


async def test_redundant_pins_are_both_flagged() -> None:
    # neither post alone breaks recovery (the other still pins) — leave-one-out would miss both.
    content = [("a", "I live in Seattle"), ("b", "from the Emerald City"), ("c", "good coffee")]

    result = await _ablate(_PinAdversary(), content)

    assert set(result.minimal_set) == {"a", "b"} and result.sufficient is True
    assert result.marginal_effect["a"] > 0 and result.marginal_effect["b"] > 0


async def test_pruning_drops_a_greedily_removed_non_cause() -> None:
    # the noise item drops confidence most, so greedy removes it first — but it isn't needed to
    # break recovery, so pruning drops it back out of the minimal set.
    content = [
        ("a", "I live in Seattle"),
        ("b", "from the Emerald City"),
        ("noise", "lots of buzzwords here"),
    ]

    result = await _ablate(_PinAdversary(), content)

    assert set(result.minimal_set) == {"a", "b"}  # the noise item is pruned
    assert result.marginal_effect["noise"] == 0.0


async def test_nothing_to_attribute_when_adversary_never_recovers() -> None:
    content = [("a", "good coffee"), ("b", "so rainy")]  # no pin → adversary abstains at baseline

    result = await _ablate(_PinAdversary(), content)

    assert result.minimal_set == [] and result.sufficient is True
    assert result.probes == 1  # one baseline probe, then it stops


async def test_probe_cap_reports_honest_failure_not_a_bogus_set() -> None:
    # redundant pins + a cap too small to break recovery: the greedily-removed items are NOT an
    # actionable edit set (editing them wouldn't fix the leak), so report couldn't-localize.
    content = [("a", "I live in Seattle"), ("b", "from the Emerald City"), ("c", "good coffee")]

    result = await find_minimal_set(
        _PinAdversary(),
        content,
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
        max_probes=2,
    )

    assert result.probes <= 2  # stops at the cap
    assert result.sufficient is False  # never broke recovery
    assert result.minimal_set == []  # no false "edit these" target
    assert all(effect == 0.0 for effect in result.marginal_effect.values())  # no bogus credit


class _UnbreakableAdversary:
    """Always recovers — the leak's signal survives even with every ablatable item removed."""

    async def adversary_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]:
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.9)],
            )
        ]


async def test_no_sufficient_subset_is_reported_honestly() -> None:
    # removing every ablatable item still recovers → "no small edit breaks this", not a fake set.
    result = await find_minimal_set(
        _UnbreakableAdversary(),
        [("a", "text one"), ("b", "text two")],
        target_attribute="location",
        true_value="Seattle, WA",
        geocoder=_FakeGeocoder(),
    )

    assert result.sufficient is False and result.minimal_set == []
    assert all(effect == 0.0 for effect in result.marginal_effect.values())
