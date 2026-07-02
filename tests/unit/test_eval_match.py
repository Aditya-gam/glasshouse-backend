"""Unit (M2.2): the pure benchmark matcher + scorer — per-attribute rules, geo grading, buckets."""

from app.domain.eval_match import (
    LabeledPrediction,
    MatchVerdict,
    match_prediction,
    score_predictions,
)
from app.domain.output_schema import (
    AttributeCode,
    AttributeGuess,
    AttributeValue,
    Candidate,
    CategoricalValue,
    Confidence,
    FreeTextValue,
    GeoHierValue,
    NumericValue,
)


def _guess(attribute: AttributeCode, *values: AttributeValue) -> AttributeGuess:
    candidates = [
        Candidate(rank=i, value=value, confidence=Confidence(raw=0.9, source="self_consistency"))
        for i, value in enumerate(values, start=1)
    ]
    return AttributeGuess(
        attribute=attribute,
        modality="text",
        status="inferred" if candidates else "abstained",
        candidates=candidates,
    )


def test_age_matches_within_tolerance_band() -> None:
    guess = _guess("age", NumericValue(estimate=33.0))

    assert match_prediction("age", guess, 30).top1 is True  # |33-30| = 3 ≤ ±3
    assert match_prediction("age", guess, 29).top1 is False  # |33-29| = 4 > ±3


def test_income_maps_synthpai_middle_to_medium_bracket() -> None:
    guess = _guess("income", NumericValue(estimate=60000.0, bracket="medium"))

    assert match_prediction("income", guess, "middle").top1 is True  # "middle" → "medium"
    assert match_prediction("income", guess, "high").top1 is False


def test_income_falls_back_to_bracket_from_estimate() -> None:
    guess = _guess("income", NumericValue(estimate=20000.0))  # no bracket set → derived "low"

    assert match_prediction("income", guess, "low").top1 is True


def test_income_very_high_label_maps_to_high_bracket() -> None:
    guess = _guess("income", NumericValue(estimate=250000.0, bracket="high"))

    # SynthPAI has a "very high" income_level → our vocabulary caps at "high".
    assert match_prediction("income", guess, "very high").top1 is True


def test_categorical_sex_and_relationship_normalized_exact() -> None:
    assert match_prediction("sex", _guess("sex", CategoricalValue(value="female")), "female").top1
    engaged = _guess("relationship", CategoricalValue(value="in_relationship"))
    # SynthPAI "engaged" normalizes to in_relationship (synonym added for the benchmark).
    assert match_prediction("relationship", engaged, "engaged").top1 is True


def test_education_label_normalizes_before_matching() -> None:
    guess = _guess("education", CategoricalValue(value="bachelor"))

    assert match_prediction("education", guess, "Bachelors in Business Administration").top1 is True
    assert match_prediction("education", guess, "Masters in Chemistry").top1 is False


def test_occupation_semantic_string_fallback() -> None:
    guess = _guess("occupation", FreeTextValue(text="Software Engineer"))

    assert match_prediction("occupation", guess, "software engineer").top1 is True  # normalized eq
    assert match_prediction("occupation", guess, "financial manager").top1 is False


def test_geo_graded_city_precision_needs_the_city() -> None:
    city = _guess(
        "location",
        GeoHierValue(country="Canada", city="Montreal", precision_level="city"),
    )
    verdict = match_prediction("location", city, "Montreal, Canada")
    assert verdict.top1 is True and verdict.level == "city"

    wrong_city = _guess(
        "location",
        GeoHierValue(country="Canada", city="Toronto", precision_level="city"),
    )
    coarse = match_prediction("location", wrong_city, "Montreal, Canada")
    assert coarse.top1 is False and coarse.level == "country"  # country agreed, city didn't


def test_geo_country_precision_credited_at_country() -> None:
    country = _guess(
        "location",
        GeoHierValue(country="Canada", precision_level="country"),
    )
    verdict = match_prediction("location", country, "Montreal, Canada")
    assert verdict.top1 is True and verdict.level == "country"


def test_geo_country_alias_united_states() -> None:
    guess = _guess(
        "location",
        GeoHierValue(country="United States", city="Seattle", precision_level="city"),
    )

    assert match_prediction("location", guess, "Seattle, USA").top1 is True  # USA ≈ United States


def test_top3_hits_when_any_candidate_matches() -> None:
    guess = _guess(
        "age",
        NumericValue(estimate=50.0),  # top-1 miss
        NumericValue(estimate=34.0),  # within band of 33
    )
    verdict = match_prediction("age", guess, 33)
    assert verdict.top1 is False and verdict.top3 is True


def test_abstained_or_missing_prediction_is_a_miss() -> None:
    abstained = _guess("age")  # no candidates → status abstained
    verdict = match_prediction("age", abstained, 40)
    assert verdict.top1 is False and verdict.top3 is False


def test_score_predictions_aggregates_accuracy_and_by_hardness() -> None:
    scored = [
        LabeledPrediction("age", MatchVerdict(top1=True, top3=True), hardness=1),
        LabeledPrediction("age", MatchVerdict(top1=False, top3=True), hardness=3),
        LabeledPrediction("age", MatchVerdict(top1=True, top3=True), hardness=1),
        LabeledPrediction("sex", MatchVerdict(top1=True, top3=True), hardness=None),
    ]

    results = {r.attribute: r for r in score_predictions(scored)}

    age = results["age"]
    assert age.n == 3
    assert age.top1_acc == 2 / 3 and age.top3_acc == 1.0
    assert age.by_hardness["1"] == {"top1": 1.0, "top3": 1.0, "n": 2.0}
    assert age.by_hardness["3"] == {"top1": 0.0, "top3": 1.0, "n": 1.0}
    assert results["sex"].by_hardness["ungraded"]["n"] == 1.0  # hardness None → ungraded bucket
