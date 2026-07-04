"""Unit (M2.4/M2.5): the pure calibration builder + the public bucket_edge seam."""

from app.domain.calibration import CalibrationSample, bucket_edge, build_calibration


def test_bucket_edge_maps_raw_to_the_lookup_bucket() -> None:
    # per-user scoring (M2.5) keys its lookup with this — must match the map's own bucketing.
    assert bucket_edge(0.0) == 0.0
    assert bucket_edge(0.67) == 0.6
    assert bucket_edge(0.7) == 0.7  # the epsilon floor: 0.7/0.1 is 6.999… in IEEE-754
    assert bucket_edge(1.0) == 0.9  # 1.0 lands in the top bucket
    assert bucket_edge(1.5) == 0.9 and bucket_edge(-0.2) == 0.0  # clamped


def _sample(
    attribute: str, raw: float, correct: bool, *, signal: str = "self_consistency", n: int = 3
) -> CalibrationSample:
    return CalibrationSample(
        attribute=attribute,  # type: ignore[arg-type]
        signal=signal,
        n=n,
        raw_confidence=raw,
        correct=correct,
    )


def test_empirical_accuracy_is_fraction_correct_per_bucket() -> None:
    # bucket [0.6,0.7): 2 of 3 correct → 0.667; bucket [0.9,1.0]: 1 of 1 correct → 1.0
    samples = [
        _sample("age", 0.67, True),
        _sample("age", 0.67, True),
        _sample("age", 0.67, False),
        _sample("age", 1.0, True),
    ]

    (curve,) = build_calibration(samples)

    by_bucket = {b.bucket: b for b in curve.buckets}
    assert round(by_bucket[0.6].empirical_accuracy, 3) == 0.667 and by_bucket[0.6].n_samples == 3
    assert by_bucket[0.9].empirical_accuracy == 1.0 and by_bucket[0.9].n_samples == 1


def test_raw_confidence_one_lands_in_the_top_bucket() -> None:
    (curve,) = build_calibration([_sample("age", 1.0, True)])

    assert [b.bucket for b in curve.buckets] == [0.9]  # 1.0 → [0.9, 1.0], not a 1.0 edge


def test_bucket_edges_floor_to_the_tenth() -> None:
    samples = [_sample("age", 0.05, True), _sample("age", 0.84, False), _sample("age", 0.9, True)]

    (curve,) = build_calibration(samples)

    assert [b.bucket for b in curve.buckets] == [0.0, 0.8, 0.9]


def test_clean_tenth_confidences_bucket_exactly() -> None:
    # IEEE-754 makes 0.3/0.1, 0.6/0.1, 0.7/0.1 truncate a bucket low without the epsilon floor;
    # these are exactly the N-run self-consistency agreement fractions (3/10, 6/10, 7/10).
    samples = [_sample("age", raw, True) for raw in (0.3, 0.6, 0.7)]

    (curve,) = build_calibration(samples)

    assert [b.bucket for b in curve.buckets] == [0.3, 0.6, 0.7]


def test_ece_is_the_weighted_confidence_accuracy_gap() -> None:
    # one bucket, mean_conf 0.8, accuracy 0.5 → ECE = |0.8 - 0.5| = 0.3
    samples = [_sample("income", 0.8, True), _sample("income", 0.8, False)]

    (curve,) = build_calibration(samples)

    assert round(curve.ece, 3) == 0.3


def test_attributes_and_signals_calibrate_separately() -> None:
    samples = [
        _sample("age", 0.9, True),
        _sample("income", 0.9, False),
        _sample("age", 0.9, True, signal="self_reported", n=1),
    ]

    curves = {(c.attribute, c.signal, c.n): c for c in build_calibration(samples)}

    assert set(curves) == {
        ("age", "self_consistency", 3),
        ("income", "self_consistency", 3),
        ("age", "self_reported", 1),
    }
    assert curves[("income", "self_consistency", 3)].buckets[0].empirical_accuracy == 0.0


def test_empty_input_yields_no_curves() -> None:
    assert build_calibration([]) == []
