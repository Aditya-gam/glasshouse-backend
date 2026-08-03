"""Unit (M5.2): assemble_finding — InferenceFinding → AttributeFindingRead, pure mapping.

Covers the evidence kind split (proven/ablation vs likely/correlational), a masked candidate value,
an abstained finding, and geo precision/neighborhood — no DB, no decryption (the repo's job).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.api.v1.inference_assembly import assemble_finding
from app.repositories.inferences import FindingCandidate, FindingEvidence, InferenceFinding

_SEATTLE = {
    "value_type": "geo_hier",
    "city": "Seattle",
    "region": "WA",
    "country": "USA",
    "precision_level": "city",
    "neighborhood": "Capitol Hill",
}


def _finding(
    *,
    candidates: list[FindingCandidate],
    evidence: list[FindingEvidence] | None = None,
    attribute: str = "location",
    reasoning: str | None = "They mention a landmark.",
) -> InferenceFinding:
    return InferenceFinding(
        attribute_code=attribute,
        status="inferred" if candidates else "abstained",
        reasoning=reasoning,
        reasoning_reveals_art9=False,
        candidates=candidates,
        evidence=evidence or [],
    )


def _evidence(**overrides: object) -> FindingEvidence:
    base: dict[str, object] = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "ref_type": "item",
        "modality": "text",
        "span": {"quote": "near Pike Place"},
        "region": None,
        "rationale": "mentions the neighborhood",
        "proxy_score": None,
        "citation_frequency": None,
        "marginal_effect": None,
        "item_text": "coffee near Pike Place",
        "posted_at": datetime(2024, 5, 1, tzinfo=UTC),
        "source": "reddit",
    }
    return FindingEvidence(**{**base, **overrides})  # type: ignore[arg-type]


def test_likely_evidence_carries_proxy_not_marginal() -> None:
    finding = _finding(
        candidates=[
            FindingCandidate(rank=1, value=_SEATTLE, calibrated_reliability=Decimal("0.76"))
        ],
        evidence=[_evidence(proxy_score=Decimal("42"), citation_frequency=Decimal("70"))],
    )

    read = assemble_finding(finding)

    evidence = read.evidence_items[0]
    assert evidence.kind == "likely"  # no ablation Δ → correlational
    assert evidence.proxy == 42.0
    assert evidence.citation == 70.0
    assert evidence.marginal is None
    assert evidence.spans == ["near Pike Place"]
    assert evidence.date == "2024-05-01"


def test_proven_evidence_carries_the_ablation_delta() -> None:
    finding = _finding(
        candidates=[FindingCandidate(rank=1, value=_SEATTLE, calibrated_reliability=None)],
        evidence=[_evidence(marginal_effect=Decimal("-0.30"))],
    )

    read = assemble_finding(finding)

    assert read.evidence_items[0].kind == "proven"  # causal ablation Δ present
    assert read.evidence_items[0].marginal == -0.30
    assert read.reliability is None  # no calibration bucket → no reliability, never a raw number


def test_masked_candidate_value_renders_empty() -> None:
    # the repo masked the Art. 9 value at the SQL source (value=None); the label must be blank.
    finding = _finding(
        candidates=[FindingCandidate(rank=1, value=None, calibrated_reliability=Decimal("0.76"))],
        attribute="birthplace",
    )

    read = assemble_finding(finding)

    assert read.value is None
    assert read.candidates[0].label == ""
    assert read.candidates[0].note == "76% reliable"  # reliability is not the sensitive value


def test_abstained_finding_has_no_value_or_reliability() -> None:
    read = assemble_finding(_finding(candidates=[], reasoning=None))

    assert read.value is None
    assert read.reliability is None
    assert read.candidates == []
    assert read.reasoning == ""


def test_geo_value_exposes_precision_and_neighborhood() -> None:
    read = assemble_finding(
        _finding(
            candidates=[
                FindingCandidate(rank=1, value=_SEATTLE, calibrated_reliability=Decimal("0.76"))
            ]
        )
    )

    assert read.value == "Seattle, WA, USA"
    assert read.precision == "city"
    assert read.neighborhood == "Capitol Hill"
