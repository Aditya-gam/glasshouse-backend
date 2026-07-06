"""Unit (M2.6): the pure eval floor gate — breach detection, missing-attribute, empty floor."""

from app.domain.eval_gate import FloorBreach, check_eval_floor


def test_passes_when_every_attribute_meets_its_floor() -> None:
    actual = {"location": 0.72, "occupation": 0.55}
    floor = {"location": 0.60, "occupation": 0.50}

    assert check_eval_floor(actual, floor) == []


def test_reports_the_attribute_below_floor() -> None:
    actual = {"location": 0.58, "occupation": 0.55}
    floor = {"location": 0.60, "occupation": 0.50}

    breaches = check_eval_floor(actual, floor)

    assert breaches == [FloorBreach(attribute="location", top1_acc=0.58, floor=0.60)]


def test_a_missing_attribute_is_a_breach_at_zero() -> None:
    # a regression that stops inferring an attribute must not slip through as "not measured".
    breaches = check_eval_floor({"location": 0.7}, {"location": 0.6, "occupation": 0.5})

    assert breaches == [FloorBreach(attribute="occupation", top1_acc=0.0, floor=0.5)]


def test_exactly_at_floor_passes() -> None:
    assert check_eval_floor({"age": 0.5}, {"age": 0.5}) == []


def test_empty_floor_never_breaches() -> None:
    assert check_eval_floor({"location": 0.0}, {}) == []  # gate not armed → pass
