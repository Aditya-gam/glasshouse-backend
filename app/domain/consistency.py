"""Self-consistency aggregation — N runs → one consensus guess (confidence-and-self-consistency.md).

Pure domain, no IO. The model's own confidence is overconfident, so the trustworthy signal is
**agreement across N runs**: cluster each run's top-1 value by meaning (the attribute's match rule),
take the majority cluster as top-1, and set `confidence.raw = the agreement fraction` over N.
Abstain when no cluster reaches the plurality threshold ⌈N/2⌉ (surfacing a near-coin-flip would be a
hallucination). The denominator is always N, so a run that omits or abstains an attribute counts
against agreement.

M1.8a clusters geo by exact canonical value and occupation by normalized string — the documented
fallbacks (§3, §8). The hierarchical-per-level geo rule and the LLM semantic occupation judge are
M1.8b.
"""

from collections.abc import Sequence
from math import ceil
from statistics import fmean

from app.domain.output_schema import (
    Agreement,
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

_AGE_TOLERANCE = 3.0  # ±3 years agree (attributes-taxonomy match rule for age)


def _geo_key(value: GeoHierValue) -> tuple[object, ...]:
    """M1.8a: exact canonical (GeoNames id if resolved, else name tuple). M1.8b: hierarchical."""
    if value.geonames_id is not None:
        return ("id", value.geonames_id)
    return ("names", value.country, value.region, value.city, value.neighborhood)


def _agree(attribute: AttributeCode, a: AttributeValue, b: AttributeValue) -> bool:
    """Do two canonical values cluster together under this attribute's match rule?"""
    if isinstance(a, CategoricalValue) and isinstance(b, CategoricalValue):
        return a.value == b.value
    if isinstance(a, NumericValue) and isinstance(b, NumericValue):
        if attribute == "income":
            return a.bracket == b.bracket  # same headline bracket agrees
        return abs(a.estimate - b.estimate) <= _AGE_TOLERANCE
    if isinstance(a, GeoHierValue) and isinstance(b, GeoHierValue):
        return _geo_key(a) == _geo_key(b)
    if isinstance(a, FreeTextValue) and isinstance(b, FreeTextValue):
        return " ".join(a.text.lower().split()) == " ".join(b.text.lower().split())
    return False  # mismatched value_types never agree (the validator forbids them per attribute)


def _self_reported(guess: AttributeGuess) -> float:
    """The model's stated confidence for this run's top-1 (the in-bucket tie-breaker, §4)."""
    confidence = guess.candidates[0].confidence
    return confidence.self_reported if confidence.self_reported is not None else confidence.raw


def _cluster(attribute: AttributeCode, picks: list[AttributeGuess]) -> list[list[AttributeGuess]]:
    """Greedy agglomerative clustering by the match rule (N is small; input order is stable)."""
    clusters: list[list[AttributeGuess]] = []
    for guess in picks:
        value = guess.candidates[0].value
        for cluster in clusters:
            if _agree(attribute, cluster[0].candidates[0].value, value):
                cluster.append(guess)
                break
        else:
            clusters.append([guess])
    return clusters


def _abstained(attribute: AttributeCode) -> AttributeGuess:
    return AttributeGuess(attribute=attribute, modality="text", status="abstained", candidates=[])


def aggregate(
    attribute: AttributeCode, guesses: Sequence[AttributeGuess], *, n_runs: int
) -> AttributeGuess:
    """Reduce the N per-run guesses for one attribute to a single self-consistency consensus."""
    picks = [g for g in guesses if g.status == "inferred" and g.candidates]
    clusters = _cluster(attribute, picks)
    if not clusters:
        return _abstained(attribute)
    # rank by cluster size, breaking ties on mean stated confidence (§4)
    ranked = sorted(
        clusters, key=lambda c: (len(c), fmean(_self_reported(g) for g in c)), reverse=True
    )
    if len(ranked[0]) < ceil(n_runs / 2):  # no plurality → abstain rather than surface a coin-flip
        return _abstained(attribute)
    candidates = [
        _to_candidate(rank, cluster, n_runs) for rank, cluster in enumerate(ranked[:3], start=1)
    ]
    top = max(ranked[0], key=_self_reported)
    return AttributeGuess(
        attribute=attribute,
        modality="text",
        status="inferred",
        candidates=candidates,
        reasoning=top.reasoning,
        reasoning_reveals_art9=top.reasoning_reveals_art9,
    )


def _to_candidate(rank: int, cluster: list[AttributeGuess], n_runs: int) -> Candidate:
    """One ranked candidate from a cluster: representative value + agreement-fraction confidence."""
    representative = max(cluster, key=_self_reported)
    fraction = len(cluster) / n_runs
    return Candidate(
        rank=rank,
        value=representative.candidates[0].value,
        confidence=Confidence(
            raw=fraction,
            source="self_consistency",
            self_reported=fmean(_self_reported(g) for g in cluster),
            agreement=Agreement(n_runs=n_runs, n_agree=len(cluster), fraction=fraction),
        ),
        evidence=representative.candidates[0].evidence,
    )
