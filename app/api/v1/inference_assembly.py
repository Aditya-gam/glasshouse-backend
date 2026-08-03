"""Attribution assembly — one `InferenceFinding` (repo) → `AttributeFindingRead` (DTO). Pure.

At the API layer because it produces wire DTOs; no IO. The Art. 9 masking already happened at the
SQL source (the repo decrypts the special-category value/reasoning only under consent), so here a
`None` value simply renders as masked/absent. Raw model confidence is never surfaced — only each
candidate's calibrated point (per-user-scoring.md separation rule).
"""

from decimal import Decimal
from typing import Any, cast

from pydantic import TypeAdapter

from app.api.v1.schemas import (
    AttributeFindingRead,
    BBox,
    Candidate,
    EvidenceRead,
    Reliability,
    Severity,
)
from app.domain.attributes import BY_CODE
from app.domain.eval_match import render_value
from app.domain.output_schema import AttributeCode, AttributeValue, GeoHierValue
from app.repositories.inferences import FindingCandidate, FindingEvidence, InferenceFinding

_VALUE_ADAPTER: TypeAdapter[AttributeValue] = TypeAdapter(AttributeValue)


def _point_band(calibrated: Decimal | None) -> Reliability | None:
    """Calibrated reliability as a point band (lo=hi until the noise model); None ⇔ no bucket."""
    if calibrated is None:
        return None
    point = float(calibrated)
    return Reliability(point=point, lo=point, hi=point)


def _value(value: dict[str, Any] | None) -> AttributeValue | None:
    """Validate a decrypted value dict into its canonical typed value, or None if masked/absent."""
    return _VALUE_ADAPTER.validate_python(value) if value is not None else None


def _note(calibrated: Decimal | None) -> str:
    """A candidate's short calibrated note (never the raw); '' when the map has no bucket."""
    return f"{round(float(calibrated) * 100)}% reliable" if calibrated is not None else ""


def _candidate(code: AttributeCode, candidate: FindingCandidate) -> Candidate:
    value = _value(candidate.value)
    label = render_value(code, value) if value is not None else ""
    return Candidate(rank=candidate.rank, label=label, note=_note(candidate.calibrated_reliability))


def _evidence(evidence: FindingEvidence) -> EvidenceRead:
    """One evidence row → EvidenceRead. `proven` (causal ablation Δ) vs `likely` (correlational)."""
    quote = evidence.span.get("quote") if evidence.span else None
    return EvidenceRead(
        id=str(evidence.id),
        kind="proven" if evidence.marginal_effect is not None else "likely",
        type="text" if evidence.modality == "text" else "photo",
        source=evidence.source or "post",
        date=evidence.posted_at.date().isoformat() if evidence.posted_at is not None else "",
        text=evidence.item_text,
        spans=[quote] if quote else None,
        region=BBox(**evidence.region) if evidence.region else None,
        rationale=evidence.rationale or "",
        marginal=float(evidence.marginal_effect) if evidence.marginal_effect is not None else None,
        proxy=float(evidence.proxy_score) if evidence.proxy_score is not None else None,
        citation=(
            float(evidence.citation_frequency) if evidence.citation_frequency is not None else None
        ),
    )


def assemble_finding(finding: InferenceFinding) -> AttributeFindingRead:
    """A decrypted finding → the attribution DTO; Art. 9 masking already applied at the source."""
    code = cast(AttributeCode, finding.attribute_code)  # FK-constrained to the 8 codes
    spec = BY_CODE[code]
    top = finding.candidates[0] if finding.candidates else None
    top_value = _value(top.value) if top is not None else None
    geo = top_value if isinstance(top_value, GeoHierValue) else None
    return AttributeFindingRead(
        code=spec.code,
        label=spec.label,
        value=render_value(code, top_value) if top_value is not None else None,
        detail=None,
        reliability=_point_band(top.calibrated_reliability) if top is not None else None,
        severity=Severity(atrisk=spec.severity.atrisk, jobseeker=spec.severity.jobseeker),
        sensitive=spec.is_sensitive_tier,
        art9=spec.is_art9,
        precision=geo.precision_level if geo is not None else None,
        neighborhood=geo.neighborhood if geo is not None else None,
        reasoning=finding.reasoning or "",
        candidates=[_candidate(code, candidate) for candidate in finding.candidates],
        text_only_reliability=None,
        evidence_items=[_evidence(evidence) for evidence in finding.evidence],
    )
