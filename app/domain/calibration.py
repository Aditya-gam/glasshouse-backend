"""Calibration map (measure/calibration.md, M2.4) — pure, no IO.

Turns the engine's raw confidence into empirical reliability: bucket the benchmark's
``(raw_confidence, correct?)`` samples by confidence **per attribute + signal + N** (attributes
calibrate differently — ``location`` ≠ ``income`` — and the map is tied to the exact self-
consistency signal it was built on), and for each bucket compute the fraction correct. The result
is the lookup ``calibrated_reliability = f(attribute, signal, n, raw)`` that per-user scoring (M2.5)
applies so a user never sees a raw model number. ``ece`` (expected calibration error) is the
per-group diagnostic. The run-to-run ``noise_std`` is a separate, multi-pass measurement, left to a
follow-up. Pinned to ``engine_version`` at persistence so a stale map is never reused.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import fmean

from app.domain.output_schema import AttributeCode

_BUCKET_WIDTH = 0.1  # 10 buckets [0.0,0.1)…[0.9,1.0]; calibration.md's confidence buckets
_NUM_BUCKETS = 10


@dataclass(frozen=True)
class CalibrationSample:
    """One benchmark top-1 prediction: raw confidence, signal + N, and whether it was correct."""

    attribute: AttributeCode
    signal: str  # self_consistency (or self_reported) — the raw signal the map is tied to
    n: int  # the self-consistency N the signal was produced at
    raw_confidence: float
    correct: bool


@dataclass(frozen=True)
class CalibrationBucket:
    """Empirical reliability for one confidence bucket."""

    bucket: float  # the bucket's lower edge (e.g. 0.8 for [0.8, 0.9))
    empirical_accuracy: float  # fraction of the bucket's predictions that were correct
    mean_confidence: float  # mean raw confidence in the bucket (for the ECE gap)
    n_samples: int  # sample count in the bucket


@dataclass(frozen=True)
class CalibrationCurve:
    """One (attribute, signal, N) calibration curve (its buckets) plus its ECE diagnostic."""

    attribute: AttributeCode
    signal: str
    n: int
    buckets: list[CalibrationBucket]
    ece: float


def _bucket_edge(raw_confidence: float) -> float:
    """The lower edge of the bucket a raw confidence falls in; 1.0 lands in the top bucket.

    The `+ 1e-9` is an epsilon-tolerant floor: `0.7 / 0.1` is `6.999999999999999` in IEEE-754, so a
    plain `int()` would file a clean-tenth confidence (exactly the N-run agreement fractions like
    7/10) one bucket too low. The epsilon (≫ the ~1e-15 float error, ≪ a bucket) corrects that.
    """
    clamped = min(max(raw_confidence, 0.0), 1.0)
    index = min(int(clamped / _BUCKET_WIDTH + 1e-9), _NUM_BUCKETS - 1)
    return round(index * _BUCKET_WIDTH, 10)


def _bucket(samples: Sequence[CalibrationSample], edge: float) -> CalibrationBucket:
    return CalibrationBucket(
        bucket=edge,
        empirical_accuracy=fmean(1.0 if sample.correct else 0.0 for sample in samples),
        mean_confidence=fmean(sample.raw_confidence for sample in samples),
        n_samples=len(samples),
    )


def build_calibration(samples: Iterable[CalibrationSample]) -> list[CalibrationCurve]:
    """Bucket the samples per (attribute, signal, N) → per-bucket empirical accuracy + the ECE.

    ECE = the sample-weighted mean gap between a bucket's mean confidence and its empirical
    accuracy (Naeini et al.); lower is better calibrated. Groups with no samples are absent.
    """
    by_group: dict[tuple[AttributeCode, str, int], list[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        by_group[(sample.attribute, sample.signal, sample.n)].append(sample)

    curves: list[CalibrationCurve] = []
    for (attribute, signal, n), group in by_group.items():
        by_bucket: dict[float, list[CalibrationSample]] = defaultdict(list)
        for sample in group:
            by_bucket[_bucket_edge(sample.raw_confidence)].append(sample)
        buckets = [_bucket(members, edge) for edge, members in sorted(by_bucket.items())]
        total = len(group)
        ece = sum(
            (bucket.n_samples / total) * abs(bucket.mean_confidence - bucket.empirical_accuracy)
            for bucket in buckets
        )
        curves.append(
            CalibrationCurve(attribute=attribute, signal=signal, n=n, buckets=buckets, ece=ece)
        )
    return sorted(curves, key=lambda curve: (curve.attribute, curve.signal, curve.n))
