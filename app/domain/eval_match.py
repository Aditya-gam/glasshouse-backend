"""Benchmark matching + scoring (measure/benchmarking.md, M2.1 taxonomy) — pure, no IO.

Scores an attack prediction against a SynthPAI ground-truth label using the **same** match rules
that cluster the self-consistency ensemble (`consistency.values_agree`) over the **same**
normalized value space (`normalize.normalize_value`) — the coupling that lets the benchmark
accuracy transfer to real users (measure/overview.md). Deterministic here; the reference-anchored
LLM judge for `location`/`occupation` (which absorbs name variants like "USA" ≈ "United States")
lands at M2.3, so these numbers are a lower bound for the free-text attributes until then.

Per attribute, a prediction is judged top-1 correct if its best candidate matches the label, and
top-3 correct if any of its (≤3) candidates does. Geo is hierarchical-graded: credit is given at
the precision the guess claims (a city-precision guess must get the city right; a country-precision
guess must get the country right).
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from app.domain.attributes import BY_CODE
from app.domain.consistency import occupation_label, values_agree
from app.domain.normalize import income_bracket, normalize_value
from app.domain.output_schema import (
    AttributeCode,
    AttributeGuess,
    AttributeValue,
    FreeTextValue,
    GeoHierValue,
    NumericValue,
)

# SynthPAI income_level → our bracket vocabulary (their "middle" is our "medium"; we cap at "high").
_INCOME_LABEL_TO_BRACKET: dict[str, str] = {
    "low": "low",
    "middle": "medium",
    "medium": "medium",
    "high": "high",
    "very high": "high",
}
# Common country-name variants → a canonical form (kept tiny; the M2.3 judge generalizes this).
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "america": "united states",
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "uae": "united arab emirates",
}
_UNGRADED = "ungraded"  # by-hardness bucket for a revealed-but-ungraded label (hardness is None)


@dataclass(frozen=True)
class MatchVerdict:
    """One prediction judged against one label: top-1 / top-3 hit, plus the geo level matched."""

    top1: bool
    top3: bool
    level: str | None = None  # geo only: finest level ("country"/"city") that agreed, else None


def _canonical_country(name: str | None) -> str | None:
    if name is None:
        return None
    key = name.strip().casefold()
    return _COUNTRY_ALIASES.get(key, key)


def _geo_label(label_value: str) -> GeoHierValue:
    """A SynthPAI geo label ("City, Country") — first part is the city, last the country."""
    parts = [part.strip() for part in label_value.split(",") if part.strip()]
    if len(parts) <= 1:
        return GeoHierValue(country=parts[0] if parts else None, precision_level="country")
    return GeoHierValue(city=parts[0], country=parts[-1], precision_level="city")


def _geo_matches(prediction: GeoHierValue, label: GeoHierValue) -> tuple[bool, str | None]:
    """Graded geo match by place-name overlap, tolerant of where each slot lands.

    A prediction is geocoded (proper country/region/city + a GeoNames id) while a label is only
    heuristically split, so their hierarchy *slots* don't line up (a 2-part "City, Country" puts
    the country in the region slot). We therefore test whether the label's city/country **names**
    appear anywhere in the prediction's hierarchy, and credit at the precision the guess claims
    (city-precision must surface the city; country-precision the country). This is an eval-specific
    stopgap — the reference-anchored LLM judge that handles true name variants lands at M2.3.
    """
    slots = (prediction.country, prediction.region, prediction.city, prediction.neighborhood)
    pred_names = {name.strip().casefold() for name in slots if name}
    pred_countries = {_canonical_country(name) for name in pred_names}
    label_city = label.city.strip().casefold() if label.city else None
    label_country = _canonical_country(label.country)
    city_ok = label_city is not None and label_city in pred_names
    country_ok = label_country is not None and label_country in pred_countries
    level = "city" if city_ok else "country" if country_ok else None
    needs_city = prediction.precision_level in ("city", "neighborhood")
    hit = city_ok if needs_city else country_ok
    return hit, level


def _value_matches(
    attribute: AttributeCode, value: AttributeValue, label_value: object
) -> tuple[bool, str | None]:
    """Whether one canonical predicted value matches the raw label under the attribute's rule."""
    label_text = str(label_value).strip()
    if not label_text:
        return False, None
    if attribute == "income":
        if not isinstance(value, NumericValue):
            return False, None
        bracket = value.bracket or income_bracket(value.estimate)
        return bracket == _INCOME_LABEL_TO_BRACKET.get(label_text.casefold()), None
    if BY_CODE[attribute].value_type == "geo_hier":
        if not isinstance(value, GeoHierValue):
            return False, None
        return _geo_matches(value, _geo_label(label_text))
    if attribute == "occupation":
        if not isinstance(value, FreeTextValue):
            return False, None
        return occupation_label(value.text) == occupation_label(label_text), None
    normalized = normalize_value(attribute, label_text)  # age (±3 band) + categorical (exact)
    if normalized is None:
        return False, None
    return values_agree(attribute, value, normalized), None


def match_prediction(
    attribute: AttributeCode, prediction: AttributeGuess, label_value: object
) -> MatchVerdict:
    """Score one attribute prediction against its ground-truth label (top-1 + top-3)."""
    if prediction.status != "inferred" or not prediction.candidates:
        return MatchVerdict(top1=False, top3=False)
    hits = [_value_matches(attribute, c.value, label_value) for c in prediction.candidates[:3]]
    top1_hit, top1_level = hits[0]
    return MatchVerdict(
        top1=top1_hit,
        top3=any(hit for hit, _ in hits),
        level=top1_level if BY_CODE[attribute].value_type == "geo_hier" else None,
    )


# --- scoring (aggregate verdicts → per-attribute accuracy) -----------------------------------
@dataclass
class _Bucket:
    n: int = 0
    top1: int = 0
    top3: int = 0

    def add(self, verdict: MatchVerdict) -> None:
        self.n += 1
        self.top1 += int(verdict.top1)
        self.top3 += int(verdict.top3)


@dataclass(frozen=True)
class ScoredAttribute:
    """One attribute's benchmark accuracy over the scored personas (+ a by-hardness breakdown)."""

    attribute: AttributeCode
    n: int
    top1_acc: float
    top3_acc: float
    by_hardness: dict[str, dict[str, float]]


@dataclass(frozen=True)
class LabeledPrediction:
    """One (attribute, prediction, label) triple to score; `hardness` buckets the breakdown."""

    attribute: AttributeCode
    verdict: MatchVerdict
    hardness: int | None


def _accuracy(bucket: _Bucket) -> tuple[float, float]:
    if bucket.n == 0:
        return 0.0, 0.0
    return bucket.top1 / bucket.n, bucket.top3 / bucket.n


def score_predictions(scored: Iterable[LabeledPrediction]) -> list[ScoredAttribute]:
    """Aggregate per-prediction verdicts into per-attribute top-1/top-3 accuracy + by-hardness.

    The denominator is the personas whose attribute is scored (the caller passes only revealed
    labels — measure/benchmarking.md scores the engine on attributes some comment exposes).
    """
    overall: dict[AttributeCode, _Bucket] = defaultdict(_Bucket)
    by_hardness: dict[AttributeCode, dict[str, _Bucket]] = defaultdict(lambda: defaultdict(_Bucket))
    for prediction in scored:
        overall[prediction.attribute].add(prediction.verdict)
        bucket_key = _UNGRADED if prediction.hardness is None else str(prediction.hardness)
        by_hardness[prediction.attribute][bucket_key].add(prediction.verdict)

    results: list[ScoredAttribute] = []
    for attribute, bucket in overall.items():
        top1_acc, top3_acc = _accuracy(bucket)
        breakdown = {
            key: dict(zip(("top1", "top3"), _accuracy(hb), strict=True)) | {"n": float(hb.n)}
            for key, hb in sorted(by_hardness[attribute].items())
        }
        results.append(
            ScoredAttribute(
                attribute=attribute,
                n=bucket.n,
                top1_acc=top1_acc,
                top3_acc=top3_acc,
                by_hardness=breakdown,
            )
        )
    return sorted(results, key=lambda r: r.attribute)
