"""Unit (M3.5): utility preservation — embedding pre-filter, meaning/readability judge, floor."""

from app.gateway.client import UtilityCriterion, UtilityGrade, UtilityVerdict
from app.services.utility import assess_utility


class _FakeEmbedder:
    """Embeds by a keyword signature so cosine reflects surface overlap (deterministic)."""

    _VOCAB = ["park", "coffee", "seattle", "great", "morning", "gone"]

    @property
    def dimension(self) -> int:
        return len(self._VOCAB)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0 if word in t.lower() else 0.0 for word in self._VOCAB] for t in texts]


class _FakeUtilityJudge:
    """Returns a fixed grade per criterion; records the calls so we can assert one-per-call."""

    def __init__(
        self, meaning: UtilityGrade = "mostly", readability: UtilityGrade = "fully"
    ) -> None:
        self._grades: dict[str, UtilityGrade] = {"meaning": meaning, "readability": readability}
        self.calls: list[str] = []

    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: UtilityCriterion
    ) -> UtilityVerdict:
        self.calls.append(criterion)
        return UtilityVerdict(reasoning="", grade=self._grades[criterion], confidence=0.9)


async def test_preserving_edit_scores_high_and_passes() -> None:
    judge = _FakeUtilityJudge(meaning="mostly", readability="fully")

    result = await assess_utility(
        judge,
        _FakeEmbedder(),
        original="a great coffee morning in Seattle park",
        edited="a great coffee morning in a park",  # dropped the city, kept the rest
        sensitive_attribute="location",
    )

    assert result.meaning == "mostly" and result.utility_score == 0.75
    assert result.readability == "fully" and result.passes_floor is True
    assert judge.calls == ["meaning", "readability"]  # one criterion per call, no halo


async def test_destructive_edit_is_prefiltered_without_calling_the_judge() -> None:
    judge = _FakeUtilityJudge()

    result = await assess_utility(
        judge,
        _FakeEmbedder(),
        original="a great coffee morning in Seattle park",
        edited="gone",  # deleted almost everything → cosine below the pre-filter
        sensitive_attribute="location",
    )

    assert result.meaning == "lost" and result.utility_score == 0.0
    assert result.readability is None and result.passes_floor is False
    assert judge.calls == []  # the judge was never spent (free pre-filter caught it)


async def test_judge_says_meaning_lost_despite_surviving_the_prefilter() -> None:
    # the surface overlap passes the embedding pre-filter, but the judge (the real verdict) says the
    # meaning is lost — embeddings gate, the judge decides.
    result = await assess_utility(
        _FakeUtilityJudge(meaning="lost"),
        _FakeEmbedder(),
        original="a great coffee morning in Seattle park",
        edited="a coffee morning in Seattle park",  # high cosine, but judged meaning-lost
        sensitive_attribute="location",
    )

    assert result.utility_score == 0.0 and result.passes_floor is False


async def test_floor_threshold_is_respected() -> None:
    original = "a great coffee morning in Seattle park"
    edited = "a coffee morning in Seattle park"
    at_floor = await assess_utility(
        _FakeUtilityJudge(meaning="partially"),
        _FakeEmbedder(),
        original=original,
        edited=edited,
        sensitive_attribute="location",
    )
    above = await assess_utility(
        _FakeUtilityJudge(meaning="partially"),
        _FakeEmbedder(),
        original=original,
        edited=edited,
        sensitive_attribute="location",
        floor=0.6,
    )

    assert at_floor.utility_score == 0.5 and at_floor.passes_floor is True  # 0.5 >= default 0.5
    assert above.passes_floor is False  # 0.5 < a 0.6 floor
