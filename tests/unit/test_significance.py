"""Unit (M3.4): paired-bootstrap significance + noise floor + value-recovery flip."""

import pytest

from app.domain.significance import assess_remediation

_BIG_DROP_BEFORE = [0.85, 0.86, 0.84, 0.85, 0.87]
_BIG_DROP_AFTER = [0.20, 0.22, 0.18, 0.21, 0.19]


def test_a_clear_drop_beats_the_noise_floor() -> None:
    delta = assess_remediation(
        _BIG_DROP_BEFORE,
        _BIG_DROP_AFTER,
        recovered_before=True,
        recovered_after=False,
        floor_margin=0.05,
    )

    assert delta.significant is True  # ~0.65 drop, CI well above the 0.05 floor
    assert delta.value_recovery_flip is True
    assert round(delta.before.point, 2) == 0.85 and round(delta.after.point, 2) == 0.20
    assert delta.before.lo <= delta.before.point <= delta.before.hi  # a real interval
    assert round(delta.drop.point, 2) == 0.65 and delta.drop.lo > 0.05


def test_a_drop_within_noise_is_not_significant() -> None:
    # a tiny drop that doesn't clear the floor → "not proven", never a quiet success.
    delta = assess_remediation(
        [0.60, 0.62, 0.61, 0.59, 0.60],
        [0.57, 0.58, 0.56, 0.59, 0.57],
        recovered_before=True,
        recovered_after=True,
        floor_margin=0.10,
    )

    assert delta.significant is False  # ~0.03 drop < 0.10 floor
    assert delta.value_recovery_flip is False  # still recovered → no flip


def test_value_recovery_flip_is_independent_of_significance() -> None:
    # the value flipped out of top-3 even though the confidence drop is within noise:
    # the flip is the headline win, the confidence is "directionally down, within noise".
    delta = assess_remediation(
        [0.55, 0.56, 0.54],
        [0.53, 0.54, 0.52],
        recovered_before=True,
        recovered_after=False,
        floor_margin=0.10,
    )

    assert delta.value_recovery_flip is True and delta.significant is False


def test_zero_variance_drop_is_still_gated_by_the_floor() -> None:
    # all-identical samples must NOT claim infinite significance — the global floor still applies.
    within = assess_remediation(
        [0.80] * 5, [0.78] * 5, recovered_before=True, recovered_after=True, floor_margin=0.05
    )
    assert within.significant is False  # a 0.02 constant drop < the 0.05 floor
    assert within.drop.lo == within.drop.hi == pytest.approx(0.02)

    beyond = assess_remediation(
        [0.80] * 5, [0.10] * 5, recovered_before=True, recovered_after=False, floor_margin=0.05
    )
    assert beyond.significant is True  # a 0.70 constant drop clears the floor


def test_deterministic_across_runs() -> None:
    first = assess_remediation(
        _BIG_DROP_BEFORE,
        _BIG_DROP_AFTER,
        recovered_before=True,
        recovered_after=False,
        floor_margin=0.05,
    )
    second = assess_remediation(
        _BIG_DROP_BEFORE,
        _BIG_DROP_AFTER,
        recovered_before=True,
        recovered_after=False,
        floor_margin=0.05,
    )

    assert first == second  # seeded bootstrap → a reproducible significance claim


def test_unequal_sample_counts_rejected() -> None:
    with pytest.raises(ValueError, match="paired experiment"):
        assess_remediation(
            [0.8, 0.8], [0.2], recovered_before=True, recovered_after=False, floor_margin=0.05
        )
