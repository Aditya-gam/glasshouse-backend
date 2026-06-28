"""Normalizer — emission `RawAttributeGuess` → canonical `AttributeGuess` (output-schema.md §6).

Deterministic, Tier-1, no LLM. This is the **pragmatic** M1.7a pass: heuristic geo split, simple
numeric/categorical parsing, occupation passthrough. Full GeoNames resolution (geonames_id) + age/
income band parsers land in M1.7b. A candidate whose value can't be parsed is dropped; if that
empties the guess, it becomes `status: abstained` (normalization never invents structure). Pure.
"""

import re

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
    RawAttributeGuess,
    RawEvidence,
    Span,
)

# Unambiguous (no ReDoS): integer part, then an optional decimal that must start with '.', so the
# two digit-runs can't overlap and backtrack (the original `[0-9,]*\.?[0-9]*` could).
_NUMBER = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?")


def _parse_number(text: str) -> float | None:
    match = _NUMBER.search(text)
    if match is None:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2)
    if suffix in ("k", "K"):
        number *= 1_000
    elif suffix in ("m", "M"):
        number *= 1_000_000
    return number


def _income_bracket(estimate: float) -> str:
    # Coarse thresholds (taxonomy owns the real ones); ~$95k reads as high (output-schema.md §5.1).
    if estimate < 40_000:
        return "low"
    return "medium" if estimate < 80_000 else "high"


def _normalize_numeric(attribute: AttributeCode, value_text: str) -> NumericValue | None:
    number = _parse_number(value_text)
    if number is None:
        return None
    if attribute == "income":
        return NumericValue(estimate=number, bracket=_income_bracket(number), unit="USD/yr")  # type: ignore[arg-type]
    return NumericValue(estimate=float(int(number)))  # age → whole years


def _normalize_categorical(attribute: AttributeCode, value_text: str) -> CategoricalValue | None:
    allowed = BY_CODE[attribute].allowed_values or ()
    lowered = value_text.strip().lower().replace(" ", "_").replace("-", "_")
    tokens = set(re.split(r"[^a-z0-9]+", lowered))
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
