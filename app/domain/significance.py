"""Noise floor + paired bootstrap (defend/noise-floor-and-variance.md, M3.4) — pure, no IO.

LLM attacks are stochastic (temperature > 0), so the **same** content re-attacked gives different
confidences run to run. A raw before→after pair therefore mixes a real effect with sampling noise.
This module decides whether a drop is **real**: treat before/after as a **paired** experiment,
bootstrap confidence intervals ("Adding Error Bars to Evals", Anthropic 2024), and call a drop
significant only if its interval clears the adversary's run-to-run **noise floor** (a margin
supplied by the caller, from the Job-1 noise model). The primary, noise-robust signal is the
**value-recovery flip** — the true value in the adversary's top-3 before, out after — with the
gated confidence drop as the magnitude.

Deterministic: the bootstrap is seeded, so the same samples always yield the same intervals (the
significance claim is reproducible). Never claims infinite significance from a lucky-identical local
sample — a zero-variance drop is still gated by the global floor margin.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

_DEFAULT_ITERATIONS = 1000
_LO_PERCENTILE = 2.5
_HI_PERCENTILE = 97.5


@dataclass(frozen=True)
class Interval:
    """A calibrated point estimate with a bootstrap confidence interval (all 0..1)."""

    point: float
    lo: float
    hi: float


@dataclass(frozen=True)
class RemediationDelta:
    """The proven before→after: intervals, whether the drop beats the noise floor, and the flip."""

    before: Interval
    after: Interval
    # the paired before−after difference (positive = the adversary got less sure)
    drop: Interval
    # the drop's CI clears the noise-floor margin (a real reduction, not sampling noise)
    significant: bool
    # true value recoverable before, not after (the headline, noise-robust safety claim)
    value_recovery_flip: bool


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Linear-interpolated percentile of `values` (percentile in 0..100)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100) * (len(ordered) - 1)
    low = int(rank)
    if low + 1 >= len(ordered):
        return ordered[-1]
    return ordered[low] + (rank - low) * (ordered[low + 1] - ordered[low])


def _bootstrap_ci(samples: Sequence[float], rng: random.Random, iterations: int) -> Interval:
    """Resample `samples` with replacement `iterations`× → a CI on the mean."""
    n = len(samples)
    means = [fmean(rng.choice(samples) for _ in range(n)) for _ in range(iterations)]  # noqa: S311
    return Interval(
        point=fmean(samples),
        lo=_percentile(means, _LO_PERCENTILE),
        hi=_percentile(means, _HI_PERCENTILE),
    )


def _paired_drop_ci(
    before: Sequence[float], after: Sequence[float], rng: random.Random, iterations: int
) -> Interval:
    """Resample the **paired** per-run differences → a CI on the drop (more power at small N)."""
    diffs = [b - a for b, a in zip(before, after, strict=True)]
    n = len(diffs)
    means = [fmean(rng.choice(diffs) for _ in range(n)) for _ in range(iterations)]  # noqa: S311
    return Interval(
        point=fmean(diffs),
        lo=_percentile(means, _LO_PERCENTILE),
        hi=_percentile(means, _HI_PERCENTILE),
    )


def assess_remediation(
    before_samples: Sequence[float],
    after_samples: Sequence[float],
    *,
    recovered_before: bool,
    recovered_after: bool,
    floor_margin: float,
    iterations: int = _DEFAULT_ITERATIONS,
    seed: int = 0,
) -> RemediationDelta:
    """Was the confidence drop real? Paired-bootstrap the N before/after adversary samples.

    `floor_margin` is the adversary's run-to-run noise floor for this (attribute, modality, level) —
    the drop is significant only if its lower CI bound strictly exceeds it (excludes zero by at
    least the margin). `recovered_before/after` come from the value-recovery check (M3.1). Requires
    the same N for both endpoints (a paired experiment).
    """
    if len(before_samples) != len(after_samples):
        raise ValueError("before/after must have the same number of samples (a paired experiment)")
    if not before_samples:
        raise ValueError("need at least one sample per endpoint")
    rng = random.Random(seed)  # noqa: S311 — statistical bootstrap, not a security context
    before = _bootstrap_ci(before_samples, rng, iterations)
    after = _bootstrap_ci(after_samples, rng, iterations)
    drop = _paired_drop_ci(before_samples, after_samples, rng, iterations)
    return RemediationDelta(
        before=before,
        after=after,
        drop=drop,
        significant=drop.lo > floor_margin,  # the drop CI clears zero by at least the noise floor
        value_recovery_flip=recovered_before and not recovered_after,
    )
