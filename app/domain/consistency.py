"""Self-consistency aggregation — N runs → one consensus guess (confidence-and-self-consistency.md).

Pure domain, no IO. The model's own confidence is overconfident, so the trustworthy signal is
**agreement across N runs**: cluster each run's top-1 value by meaning (the attribute's match rule),
take the majority cluster as top-1, and set `confidence.raw = the agreement fraction` over N.
Abstain when no cluster reaches the plurality threshold ⌈N/2⌉ (surfacing a near-coin-flip would be a
hallucination). The denominator is always N, so a run that omits or abstains an attribute counts
against agreement.

`geo_hier` clusters **hierarchically** (§3): agreement is measured at each level, and the top-1 is
reported at the finest level that clears the threshold — the principled source of `precision_level`
("pinned to your state" vs "your block"). `occupation` clustering needs a semantic judge (IO), so it
runs at the service layer (`app.services.occupation`) and reuses the pure `build_consensus` here;
the `_agree` occupation rule is the §8 normalized-string fallback.
"""

from collections import defaultdict
from collections.abc import Sequence
from math import ceil
from statistics import fmean
from typing import Literal

from app.domain.attributes import BY_CODE
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

_PrecisionLevel = Literal["country", "region", "city", "neighborhood"]
_AGE_TOLERANCE = 3.0  # ±3 years agree (attributes-taxonomy match rule for age)
_GEO_LEVELS: tuple[_PrecisionLevel, ...] = (
    "country",
    "region",
    "city",
    "neighborhood",
)  # coarse→fine


def _geo_key(value: GeoHierValue) -> tuple[object, ...]:
    """Exact-canonical geo identity (GeoNames id if resolved, else name tuple) — for runner-ups."""
    if value.geonames_id is not None:
        return ("id", value.geonames_id)
    return ("names", value.country, value.region, value.city, value.neighborhood)


def _agree(attribute: AttributeCode, a: AttributeValue, b: AttributeValue) -> bool:
    """Do two canonical values cluster together under this attribute's (exact) match rule?"""
    if isinstance(a, CategoricalValue) and isinstance(b, CategoricalValue):
        return a.value == b.value
    if isinstance(a, NumericValue) and isinstance(b, NumericValue):
        if attribute == "income":
            return a.bracket == b.bracket  # same headline bracket agrees
        return abs(a.estimate - b.estimate) <= _AGE_TOLERANCE
    if isinstance(a, GeoHierValue) and isinstance(b, GeoHierValue):
        return _geo_key(a) == _geo_key(b)
    if isinstance(a, FreeTextValue) and isinstance(b, FreeTextValue):
        return _occupation_label(a.text) == _occupation_label(b.text)
    return False  # mismatched value_types never agree (the validator forbids them per attribute)


def _occupation_label(text: str) -> str:
    """Normalized occupation string — the §8 fallback when the semantic judge is unavailable."""
    return " ".join(text.lower().split())


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


def _rank(clusters: list[list[AttributeGuess]]) -> list[list[AttributeGuess]]:
    """Order clusters by size, breaking ties on mean stated confidence (§4)."""
    return sorted(
        clusters, key=lambda c: (len(c), fmean(_self_reported(g) for g in c)), reverse=True
    )


def _abstained(attribute: AttributeCode) -> AttributeGuess:
    return AttributeGuess(attribute=attribute, modality="text", status="abstained", candidates=[])


def build_consensus(
    attribute: AttributeCode, clusters: Sequence[list[AttributeGuess]], *, n_runs: int
) -> AttributeGuess:
    """Ranked clusters → consensus (shared by the deterministic rules + the occupation judge)."""
    if not clusters:
        return _abstained(attribute)
    ranked = _rank(list(clusters))
    if len(ranked[0]) < ceil(n_runs / 2):  # no plurality → abstain rather than surface a coin-flip
        return _abstained(attribute)
    candidates = [
        _to_candidate(rank, cluster, n_runs) for rank, cluster in enumerate(ranked[:3], 1)
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


def aggregate(
    attribute: AttributeCode, guesses: Sequence[AttributeGuess], *, n_runs: int
) -> AttributeGuess:
    """Reduce the N per-run guesses for one (non-occupation) attribute to a single consensus."""
    picks = [g for g in guesses if g.status == "inferred" and g.candidates]
    if BY_CODE[attribute].value_type == "geo_hier":
        return _aggregate_geo(attribute, picks, n_runs=n_runs)
    return build_consensus(attribute, _cluster(attribute, picks), n_runs=n_runs)


def _to_candidate(rank: int, cluster: list[AttributeGuess], n_runs: int) -> Candidate:
    """One ranked candidate from a cluster: representative value + agreement-fraction confidence."""
    representative = max(cluster, key=_self_reported)
    return _candidate(rank, representative.candidates[0].value, cluster, representative, n_runs)


def _candidate(
    rank: int,
    value: AttributeValue,
    cluster: list[AttributeGuess],
    representative: AttributeGuess,
    n_runs: int,
) -> Candidate:
    fraction = len(cluster) / n_runs
    return Candidate(
        rank=rank,
        value=value,
        confidence=Confidence(
            raw=fraction,
            source="self_consistency",
            self_reported=fmean(_self_reported(g) for g in cluster),
            agreement=Agreement(n_runs=n_runs, n_agree=len(cluster), fraction=fraction),
        ),
        evidence=representative.candidates[0].evidence,
    )


# --- hierarchical geo (§3, §5) ---------------------------------------------------------------
def _geo_chain(value: GeoHierValue, depth: int) -> tuple[str | None, ...]:
    """The normalized hierarchy down to `depth` (0=country … 3=neighborhood)."""
    fields = (value.country, value.region, value.city, value.neighborhood)
    return tuple(f.strip().lower() if f else None for f in fields[: depth + 1])


def _aggregate_geo(
    attribute: AttributeCode, picks: list[AttributeGuess], *, n_runs: int
) -> AttributeGuess:
    """Cluster geo by meaning at each level; report top-1 at the finest level that clears ⌈N/2⌉."""
    threshold = ceil(n_runs / 2)
    max_depth = (
        2 if attribute == "birthplace" else 3
    )  # birthplace hierarchy is {country, region, city}
    chosen: tuple[int, list[int]] | None = None
    for depth in range(max_depth, -1, -1):  # finest → coarsest; stop at the first level that clears
        groups: dict[tuple[str | None, ...], list[int]] = defaultdict(list)
        for index, guess in enumerate(picks):
            chain = _geo_chain(_geo_value(guess), depth)
            if chain[-1] is not None:  # the run resolved a value at this level
                groups[chain].append(index)
        if groups:
            _, indices = max(groups.items(), key=lambda kv: len(kv[1]))
            if len(indices) >= threshold:
                chosen = (depth, indices)
                break
    if chosen is None:
        return _abstained(attribute)
    depth, member_indices = chosen
    members = [picks[i] for i in member_indices]
    top = _geo_top_candidate(1, depth, members, n_runs)
    remaining = [picks[i] for i in range(len(picks)) if i not in set(member_indices)]
    runners = [
        _to_candidate(rank, cluster, n_runs)
        for rank, cluster in enumerate(_rank(_cluster(attribute, remaining))[:2], start=2)
    ]
    representative = max(members, key=_self_reported)
    return AttributeGuess(
        attribute=attribute,
        modality="text",
        status="inferred",
        candidates=[top, *runners],
        reasoning=representative.reasoning,
        reasoning_reveals_art9=representative.reasoning_reveals_art9,
    )


def _geo_value(guess: AttributeGuess) -> GeoHierValue:
    value = guess.candidates[0].value
    if not isinstance(value, GeoHierValue):  # the validator guarantees geo_hier; guard for the type
        raise TypeError("expected a geo_hier value")
    return value


def _geo_top_candidate(
    rank: int, depth: int, members: list[AttributeGuess], n_runs: int
) -> Candidate:
    """Top-1 truncated to the agreed level; keep a `geonames_id` only if a member resolved there."""
    level: _PrecisionLevel = _GEO_LEVELS[depth]
    # prefer a member resolved exactly at this level so its geonames_id names this place
    representative = next(
        (m for m in members if _geo_value(m).precision_level == level),
        max(members, key=_self_reported),
    )
    source = _geo_value(representative)
    keep_id = source.precision_level == level
    value = GeoHierValue(
        country=source.country,
        region=source.region if depth >= 1 else None,
        city=source.city if depth >= 2 else None,
        neighborhood=source.neighborhood if depth >= 3 else None,
        precision_level=level,
        geonames_id=source.geonames_id if keep_id else None,
    )
    return _candidate(rank, value, members, representative, n_runs)
