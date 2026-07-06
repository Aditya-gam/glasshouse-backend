"""Unit (M3.5): utility preservation — embedding pre-filter, meaning/readability judge, floor."""

from app.gateway.client import UtilityCriterion, UtilityGrade, UtilityVerdict
from app.gateway.prompts import build_utility_prompt
from app.services.utility import assess_utility


class _HighFloorEmbedder:
    """Models the real bge geometry: a HIGH similarity floor (even unrelated text scores ~0.9), so
    the length guard — not the cosine — must catch a gutted/empty edit."""

    @property
    def dimension(self) -> int:
        return 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.9, 0.9] for _ in texts]  # everything is ~cosine 0.99 to everything


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


async def test_gutted_edit_is_prefiltered_by_the_length_guard() -> None:
    # even with a HIGH-floor embedder (like real bge, where an empty edit still scores ~0.99), the
    # length guard catches the delete-by-cheat — the judge is never spent.
    judge = _FakeUtilityJudge()

    result = await assess_utility(
        judge,
        _HighFloorEmbedder(),
        original="a great coffee morning in Seattle park",
        edited="",  # delete-everything
        sensitive_attribute="location",
    )

    assert result.meaning == "lost" and result.utility_score == 0.0
    assert result.readability is None and result.passes_floor is False
    assert judge.calls == []  # the judge was never spent (free length-guard caught it)


async def test_unrelated_edit_is_prefiltered_by_cosine() -> None:
    judge = _FakeUtilityJudge()

    result = await assess_utility(
        judge,
        _FakeEmbedder(),
        original="a great coffee morning in Seattle park",
        edited="gone gone gone gone gone",  # long enough to pass length, but zero token overlap
        sensitive_attribute="location",
    )

    assert result.meaning == "lost" and judge.calls == []  # cosine 0 < the pre-filter


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


def test_readability_prompt_is_blind_to_the_original_and_attribute() -> None:
    # the fluency judge must NOT see the original or the sensitive attribute (avoid halo); the
    # meaning judge does. The blindness lives entirely in build_utility_prompt's branch.
    readability = build_utility_prompt(
        original="I live near the Space Needle",
        edited="I live nearby",
        attribute="location",
        criterion="readability",
    )
    meaning = build_utility_prompt(
        original="I live near the Space Needle",
        edited="I live nearby",
        attribute="location",
        criterion="meaning",
    )

    assert "I live nearby" in readability
    assert "Space Needle" not in readability and "location" not in readability  # blind
    assert "Space Needle" in meaning and "location" in meaning  # meaning sees both
