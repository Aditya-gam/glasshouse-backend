"""Unit (M1.7a): the normalizer maps emission RawAttributeGuess → canonical AttributeGuess."""

from app.domain.normalize import normalize_guess
from app.domain.output_schema import (
    AttributeCode,
    CategoricalValue,
    FreeTextValue,
    GeoHierValue,
    NumericValue,
    RawAttributeGuess,
    RawCandidate,
    RawEvidence,
)


def _raw(
    attribute: AttributeCode,
    value_text: str,
    *,
    confidence: float = 0.8,
    evidence: list[RawEvidence] | None = None,
) -> RawAttributeGuess:
    return RawAttributeGuess(
        attribute=attribute,
        status="inferred",
        candidates=[
            RawCandidate(value_text=value_text, self_confidence=confidence, evidence=evidence or [])
        ],
    )


def test_geo_hier_splits_place() -> None:
    value = normalize_guess(_raw("location", "Seattle, Washington, US")).candidates[0].value
    assert isinstance(value, GeoHierValue)
    assert (value.city, value.region, value.country) == ("Seattle", "Washington", "US")
    assert value.precision_level == "city" and value.geonames_id is None


def test_income_parses_to_estimate_and_bracket() -> None:
    value = normalize_guess(_raw("income", "about $95k")).candidates[0].value
    assert isinstance(value, NumericValue)
    assert value.estimate == 95000 and value.bracket == "high" and value.unit == "USD/yr"


def test_age_parses_to_whole_years() -> None:
    value = normalize_guess(_raw("age", "I'm 31")).candidates[0].value
    assert isinstance(value, NumericValue) and value.estimate == 31


def test_categorical_matches_allowed() -> None:
    value = normalize_guess(_raw("relationship", "married")).candidates[0].value
    assert isinstance(value, CategoricalValue) and value.value == "married"


def test_categorical_female_does_not_match_male() -> None:
    value = normalize_guess(_raw("sex", "female")).candidates[0].value
    assert isinstance(value, CategoricalValue) and value.value == "female"


def test_categorical_unknown_fallback() -> None:
    value = normalize_guess(_raw("sex", "prefer not to say")).candidates[0].value
    assert isinstance(value, CategoricalValue) and value.value == "unknown"


def test_occupation_passthrough() -> None:
    value = normalize_guess(_raw("occupation", "backend software engineer")).candidates[0].value
    assert isinstance(value, FreeTextValue) and value.text == "backend software engineer"


def test_confidence_is_self_reported() -> None:
    confidence = normalize_guess(_raw("age", "31", confidence=0.7)).candidates[0].confidence
    assert confidence.source == "self_reported"
    assert (
        confidence.raw == 0.7 and confidence.self_reported == 0.7 and confidence.agreement is None
    )


def test_unparseable_numeric_abstains() -> None:
    guess = normalize_guess(_raw("age", "no idea"))
    assert guess.status == "abstained" and guess.candidates == []


def test_evidence_is_mapped() -> None:
    raw = _raw(
        "location",
        "Seattle, WA",
        evidence=[RawEvidence(ref_id="itm_1", quote="Gas Works Park", rationale="park")],
    )
    evidence = normalize_guess(raw).candidates[0].evidence[0]
    assert (
        evidence.ref_type == "item" and evidence.ref_id == "itm_1" and evidence.modality == "text"
    )
    assert evidence.span is not None and evidence.span.quote == "Gas Works Park"
    assert evidence.rationale == "park"
