"""Unit (M2.2): `_score_persona` — the certainty gate + missing-prediction handling in the eval.

Guards the invariant that only labels a comment reveals (certainty ≥ 1) enter the accuracy
denominator (benchmarking.md), and that a revealed label the engine never guessed is a miss.
"""

from app.domain.output_schema import (
    AttributeGuess,
    Candidate,
    CategoricalValue,
    Confidence,
)
from app.repositories.eval_labels import EvalLabelRow
from app.services.eval import _score_persona


def _sex_guess(value: str) -> AttributeGuess:
    return AttributeGuess(
        attribute="sex",
        modality="text",
        status="inferred",
        candidates=[
            Candidate(
                rank=1,
                value=CategoricalValue(value=value),
                confidence=Confidence(raw=0.9, source="self_consistency"),
            )
        ],
    )


def _label(
    attribute: str, value: object, *, certainty: int, hardness: int | None = 2
) -> EvalLabelRow:
    return EvalLabelRow(
        attribute_code=attribute,
        true_value={"value": value, "hardness": hardness, "certainty": certainty},
    )


def test_unrevealed_labels_are_excluded_from_scoring() -> None:
    guesses = [_sex_guess("female")]
    labels = [
        _label("sex", "female", certainty=3),  # revealed → scored
        _label("age", 40, certainty=0),  # never revealed → dropped from the denominator
        _label("income", "middle", certainty=0),  # dropped
    ]

    scored = _score_persona(guesses, labels)

    assert [p.attribute for p in scored] == ["sex"]  # only the revealed attribute counts
    assert scored[0].verdict.top1 is True


def test_revealed_label_with_no_prediction_is_a_miss() -> None:
    labels = [_label("sex", "female", certainty=3)]  # revealed, but the engine returned nothing

    scored = _score_persona([], labels)

    assert len(scored) == 1
    assert scored[0].verdict.top1 is False and scored[0].verdict.top3 is False


def test_hardness_none_is_kept_when_revealed() -> None:
    labels = [_label("sex", "female", certainty=2, hardness=None)]  # revealed but ungraded

    scored = _score_persona([_sex_guess("female")], labels)

    assert len(scored) == 1 and scored[0].hardness is None
