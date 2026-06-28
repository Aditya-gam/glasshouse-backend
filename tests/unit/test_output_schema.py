"""Unit: the Profiler emission contract (RawAttributeGuess) validates and rejects correctly."""

import pytest
from pydantic import ValidationError

from app.domain.output_schema import RawAttributeGuess, RawCandidate, RawEvidence


def test_validates_a_well_formed_guess() -> None:
    guess = RawAttributeGuess(
        attribute="location",
        status="inferred",
        candidates=[
            RawCandidate(
                value_text="Seattle, Washington, US",
                self_confidence=0.81,
                evidence=[RawEvidence(ref_id="itm_4471", quote="Gas Works Park", rationale="park")],
            )
        ],
        reasoning="names Seattle-specific places",
    )
    assert guess.attribute == "location"
    assert guess.candidates[0].self_confidence == 0.81


def test_abstained_defaults_to_no_candidates() -> None:
    guess = RawAttributeGuess(attribute="sex", status="abstained")
    assert guess.candidates == []


def test_rejects_confidence_out_of_range() -> None:
    with pytest.raises(ValidationError):
        RawCandidate.model_validate({"value_text": "x", "self_confidence": 1.5})


def test_rejects_unknown_attribute() -> None:
    with pytest.raises(ValidationError):
        RawAttributeGuess.model_validate({"attribute": "hobby", "status": "inferred"})


def test_rejects_more_than_three_candidates() -> None:
    with pytest.raises(ValidationError):
        RawAttributeGuess.model_validate(
            {
                "attribute": "age",
                "status": "inferred",
                "candidates": [{"value_text": str(i), "self_confidence": 0.5} for i in range(4)],
            }
        )
