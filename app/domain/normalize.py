"""Normalizer — emission `RawAttributeGuess` → canonical `AttributeGuess` (output-schema.md §6).

Deterministic, Tier-1, no LLM, **pure** (no IO). Numeric band parsers ("late 20s"→28, "~$95k"→
bracket high), a categorical synonym layer ("hitched"→married), the heuristic geo split, and
occupation passthrough. GeoNames resolution of the geo split (`geonames_id`, trustworthy
`precision_level`) is IO → it runs at the service layer (`app.services.geocoding.enrich_geo`). A
candidate whose value can't be parsed is dropped; if that empties the guess it becomes
`status: abstained` (normalization never invents structure the model didn't assert).
"""

import re
from typing import Literal

from app.domain.attributes import BY_CODE
from app.domain.output_schema import (
    AttributeCode,
    AttributeGuess,
    AttributeValue,
    Candidate,
    CategoricalValue,
    Confidence,
    Evidence,
    FreeTextValue,
    GeoHierValue,
    NumericValue,
    Range,
    RawAttributeGuess,
    RawEvidence,
    Span,
)


def _parse_number(text: str) -> float | None:
    """Extract the first number (commas + optional decimal, k/m suffix). Regex-free → no ReDoS."""
    cleaned = text.replace(",", "")
    start = next((i for i, char in enumerate(cleaned) if char.isdigit()), None)
    if start is None:
        return None
    index = start
    seen_dot = False
    while index < len(cleaned) and (
        cleaned[index].isdigit() or (cleaned[index] == "." and not seen_dot)
    ):
        seen_dot = seen_dot or cleaned[index] == "."
        index += 1
    number = float(cleaned[start:index].rstrip("."))
    suffix = cleaned[index] if index < len(cleaned) else ""
    if suffix in ("k", "K"):
        return number * 1_000
    if suffix in ("m", "M"):
        return number * 1_000_000
    return number


def _income_bracket(estimate: float) -> Literal["low", "medium", "high"]:
    # Coarse thresholds (taxonomy owns the real ones); ~$95k reads as high (output-schema.md §5.1).
    if estimate < 40_000:
        return "low"
    return "medium" if estimate < 80_000 else "high"


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens — a linear char-class split (no backtracking → no ReDoS)."""
    return [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]


# --- age band parsing ("late 20s" → 28, range [27,29]; output-schema.md §5.1) ---------------
_WORD_DECADES = {
    "twenties": 20,
    "thirties": 30,
    "forties": 40,
    "fifties": 50,
    "sixties": 60,
    "seventies": 70,
    "eighties": 80,
    "nineties": 90,
}
# modifier → (offset from the decade base, half-width of the range)
_DECADE_MODIFIERS = {"early": (2, 1), "mid": (5, 1), "late": (8, 1)}


def _decade_base(tokens: list[str]) -> int | None:
    """The decade a phrase refers to: 'thirties'/'30s' → 30; None if no decade is named."""
    for token in tokens:
        if token in _WORD_DECADES:
            return _WORD_DECADES[token]
        if len(token) >= 3 and token.endswith("s") and token[:-1].isdigit():
            number = int(token[:-1])
            if number % 10 == 0 and 20 <= number <= 90:  # "20s".."90s"
                return number
    return None


def _parse_age(value_text: str) -> NumericValue | None:
    tokens = _tokens(value_text)
    base = _decade_base(tokens)
    if base is not None:
        for modifier, (offset, half) in _DECADE_MODIFIERS.items():
            if modifier in tokens:
                estimate = float(base + offset)
                return NumericValue(
                    estimate=estimate, range=Range(low=estimate - half, high=estimate + half)
                )
        # bare decade → midpoint estimate spanning the full decade
        return NumericValue(
            estimate=float(base + 5), range=Range(low=float(base), high=float(base + 9))
        )
    number = _parse_number(value_text)
    return NumericValue(estimate=float(int(number))) if number is not None else None  # whole years


# --- income parsing ("six figures" → high; "80k-100k" → band; "~$95k" → estimate + band) -----
_WORD_FIGURES = {"four": 4, "five": 5, "six": 6, "seven": 7}
# figure count → (representative estimate, low, high) of the order-of-magnitude band
_FIGURE_BANDS = {
    4: (5_000.0, 1_000.0, 9_999.0),
    5: (50_000.0, 10_000.0, 99_999.0),
    6: (150_000.0, 100_000.0, 999_999.0),
    7: (1_500_000.0, 1_000_000.0, 9_999_999.0),
}
_APPROX_MARKERS = ("~", "about", "around", "approx", "roughly", "ish")


def _figure_band(tokens: list[str]) -> NumericValue | None:
    if "figures" not in tokens and "figure" not in tokens:
        return None
    count = next((_WORD_FIGURES[t] for t in tokens if t in _WORD_FIGURES), None)
    if count is None:
        count = next((int(t) for t in tokens if t.isdigit() and int(t) in _FIGURE_BANDS), None)
    if count is None:
        return None
    estimate, low, high = _FIGURE_BANDS[count]
    return NumericValue(
        estimate=estimate,
        range=Range(low=low, high=high),
        bracket=_income_bracket(estimate),
        unit="USD/yr",
    )


def _two_numbers(value_text: str) -> tuple[float, float] | None:
    """Parse an explicit range ('80k-100k', '80k to 100k', 'between 80 and 100k')."""
    lowered = value_text.lower()
    for separator in (" to ", " and ", "-", "–", "—"):
        if separator in lowered:
            left, _, right = lowered.partition(separator)
            low, high = _parse_number(left), _parse_number(right)
            if low is not None and high is not None and low <= high:
                return low, high
    return None


def _parse_income(value_text: str) -> NumericValue | None:
    figure = _figure_band(_tokens(value_text))
    if figure is not None:
        return figure
    span = _two_numbers(value_text)
    if span is not None:
        low, high = span
        estimate = (low + high) / 2
        return NumericValue(
            estimate=estimate,
            range=Range(low=low, high=high),
            bracket=_income_bracket(estimate),
            unit="USD/yr",
        )
    number = _parse_number(value_text)
    if number is None:
        return None
    band: Range | None = None
    if any(marker in value_text.lower() for marker in _APPROX_MARKERS):  # only band when hedged
        band = Range(
            low=float(round(number * 0.85 / 1000) * 1000),
            high=float(round(number * 1.15 / 1000) * 1000),
        )
    return NumericValue(estimate=number, range=band, bracket=_income_bracket(number), unit="USD/yr")


def _normalize_numeric(attribute: AttributeCode, value_text: str) -> NumericValue | None:
    return _parse_income(value_text) if attribute == "income" else _parse_age(value_text)


# Colloquial → canonical, per attribute (output-schema.md §6). Each target is an allowed value.
# fmt: off
_CATEGORICAL_SYNONYMS: dict[AttributeCode, dict[str, str]] = {
    "sex": {
        "woman": "female", "she": "female", "she_her": "female", "girl": "female", "f": "female",
        "man": "male", "he": "male", "he_him": "male", "guy": "male", "boy": "male", "m": "male",
        "they_them": "non-binary", "nonbinary": "non-binary", "enby": "non-binary", "nb": "non-binary",  # noqa: E501
    },
    "relationship": {
        "hitched": "married", "wed": "married", "spouse": "married", "husband": "married", "wife": "married",  # noqa: E501
        "dating": "in_relationship", "partnered": "in_relationship", "taken": "in_relationship",
        "gf": "in_relationship", "bf": "in_relationship", "boyfriend": "in_relationship", "girlfriend": "in_relationship",  # noqa: E501
        "engaged": "in_relationship", "in_a_relationship": "in_relationship",  # SynthPAI labels; exact phrase only (a bare "relationship" token would hijack "complicated relationship")  # noqa: E501
        "solo": "single", "unattached": "single",
        "separated": "complicated", "widow": "widowed", "widower": "widowed",
    },
    "education": {
        "phd": "doctorate", "doctoral": "doctorate", "dphil": "doctorate",
        "masters": "master", "mba": "master", "ms": "master", "ma": "master",
        "undergrad": "bachelor", "bachelors": "bachelor", "ba": "bachelor", "bs": "bachelor", "bsc": "bachelor",  # noqa: E501
        "associates": "associate", "aa": "associate", "ged": "high_school", "hs": "high_school",
        "jd": "professional", "md": "professional",
    },
}
# fmt: on


def _normalize_categorical(attribute: AttributeCode, value_text: str) -> CategoricalValue | None:
    allowed = BY_CODE[attribute].allowed_values or ()
    lowered = re.sub(r"[^a-z0-9]+", "_", value_text.strip().lower()).strip("_")
    tokens = set(lowered.split("_"))
    synonyms = _CATEGORICAL_SYNONYMS.get(attribute, {})
    # synonym layer first: exact normalized form, then any single token ("she/her" → female).
    mapped = synonyms.get(lowered) or next((synonyms[t] for t in tokens if t in synonyms), None)
    if mapped is not None:
        return CategoricalValue(value=mapped)
    for value in allowed:
        norm = value.replace("-", "_")
        # exact / prefix / whole-token — never a loose substring ("male" must not match "female").
        if lowered == norm or lowered.startswith(norm) or norm in tokens:
            return CategoricalValue(value=value)
    return CategoricalValue(value="unknown") if "unknown" in allowed else None


def _normalize_geo(value_text: str) -> GeoHierValue | None:
    parts = [part.strip() for part in value_text.split(",") if part.strip()]
    if not parts:
        return None
    return GeoHierValue(
        city=parts[0],
        region=parts[1] if len(parts) >= 2 else None,
        country=parts[-1] if len(parts) >= 3 else None,
        precision_level="city",  # best-effort until GeoNames resolves depth (M1.7b)
    )


def _normalize_value(attribute: AttributeCode, value_text: str) -> AttributeValue | None:
    value_type = BY_CODE[attribute].value_type
    if value_type == "numeric":
        return _normalize_numeric(attribute, value_text)
    if value_type == "categorical":
        return _normalize_categorical(attribute, value_text)
    if value_type == "geo_hier":
        return _normalize_geo(value_text)
    text = value_text.strip()
    return FreeTextValue(text=text) if text else None


def normalize_value(attribute: AttributeCode, value_text: str) -> AttributeValue | None:
    """Canonicalize one raw value string for `attribute` (public seam; eval reuses it on labels).

    The eval matcher (M2.2) normalizes a benchmark label through the *same* parser the attack
    uses, so a prediction and its ground-truth label are compared in one canonical space — the
    coupling that lets benchmark accuracy transfer.
    """
    return _normalize_value(attribute, value_text)


def income_bracket(estimate: float) -> Literal["low", "medium", "high"]:
    """The coarse income bracket for a numeric estimate (public seam; eval reuses it)."""
    return _income_bracket(estimate)


def _to_evidence(raw: RawEvidence) -> Evidence:
    return Evidence(
        ref_type="item",  # text attack reads only `items`; image refs arrive at M4
        ref_id=raw.ref_id,
        modality="text",
        span=Span(quote=raw.quote) if raw.quote else None,
        rationale=raw.rationale,
    )


def normalize_guess(raw: RawAttributeGuess) -> AttributeGuess:
    """Normalize one emission guess to canonical; unparseable candidates drop → maybe abstained."""
    candidates: list[Candidate] = []
    for rank, raw_candidate in enumerate(raw.candidates, start=1):
        value = _normalize_value(raw.attribute, raw_candidate.value_text)
        if value is None:
            continue
        candidates.append(
            Candidate(
                rank=rank,
                value=value,
                confidence=Confidence(
                    raw=raw_candidate.self_confidence,
                    source="self_reported",  # self-consistency raw arrives at M1.8
                    self_reported=raw_candidate.self_confidence,
                ),
                evidence=[_to_evidence(e) for e in raw_candidate.evidence],
            )
        )
    return AttributeGuess(
        attribute=raw.attribute,
        modality="text",
        status="inferred" if candidates else "abstained",
        candidates=candidates,
        reasoning=raw.reasoning,
    )
