"""Unit (M3.3): the anonymizer loop — refine-against-feedback, generalization-first, k-hop cap."""

from app.domain.output_schema import RawAttributeGuess, RawCandidate
from app.gateway.client import AnonymizerEdit
from app.services.anonymize import anonymize_item
from app.services.geocoding import GeoResolution

_MARKERS = ("Seattle", "Space Needle")  # each independently pins the city


class _FakeGeocoder:
    async def resolve(self, place: str) -> GeoResolution | None:
        return None


class _GeneralizingAnonymizer:
    """Removes the first surviving pin marker each hop (a stand-in for generalize-the-cue)."""

    def __init__(self) -> None:
        self.feedback_seen: list[str | None] = []

    async def anonymize(
        self,
        *,
        text: str,
        spans: list[str],
        attribute: str,
        feedback: str | None = None,
        strength: str = "minimal",
    ) -> AnonymizerEdit:
        self.feedback_seen.append(feedback)
        edited = text
        for marker in _MARKERS:
            if marker in edited:
                edited = edited.replace(marker, "somewhere")
                break
        return AnonymizerEdit(
            reasoning="generalize the cue", operation="generalize", edited_text=edited, note="ok"
        )


class _StubbornAnonymizer:
    """Never actually removes the cue — used to exercise the k-hop cap / not-broken path."""

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


async def _feedback_profile_fn(content: str, temperature: float) -> list[RawAttributeGuess]:
    """The feedback adversary: recovers 'Seattle, WA' iff a pin marker survives in the content."""
    if any(marker in content for marker in _MARKERS):
        return [
            RawAttributeGuess(
                attribute="location",
                status="inferred",
                candidates=[RawCandidate(value_text="Seattle, WA", self_confidence=0.9)],
            )
        ]
    return [RawAttributeGuess(attribute="location", status="abstained", candidates=[])]


async def test_loop_refines_until_the_feedback_adversary_is_broken() -> None:
    anonymizer = _GeneralizingAnonymizer()

    result = await anonymize_item(
        anonymizer,
        _feedback_profile_fn,
        ("itm1", "I live near Seattle by the Space Needle"),
        target_attribute="location",
        true_value="Seattle, WA",
        spans=["Seattle", "Space Needle"],
        geocoder=_FakeGeocoder(),
    )

    assert result.broke is True
    assert result.hops == 2  # one cue removed per hop; two cues → two hops
    assert "Seattle" not in result.edited_text and "Space Needle" not in result.edited_text
    assert result.original_text == "I live near Seattle by the Space Needle"  # original preserved
    # the first hop has no feedback; the second is refined against the adversary's feedback.
    assert anonymizer.feedback_seen[0] is None
    assert anonymizer.feedback_seen[1] is not None


async def test_loop_reports_not_broken_at_the_hop_cap() -> None:
    result = await anonymize_item(
        _StubbornAnonymizer(),
        _feedback_profile_fn,
        ("itm1", "I live near Seattle"),
        target_attribute="location",
        true_value="Seattle, WA",
        spans=["Seattle"],
        geocoder=_FakeGeocoder(),
        hops=3,
    )

    assert result.broke is False and result.hops == 3  # honest failure, no false success
    assert "Seattle" in result.edited_text  # nothing was actually removed


async def test_single_hop_break_stops_early() -> None:
    result = await anonymize_item(
        _GeneralizingAnonymizer(),
        _feedback_profile_fn,
        ("itm1", "I live near Seattle"),  # one cue → one hop
        target_attribute="location",
        true_value="Seattle, WA",
        spans=["Seattle"],
        geocoder=_FakeGeocoder(),
    )

    assert result.broke is True and result.hops == 1
