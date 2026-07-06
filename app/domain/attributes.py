"""The 8-attribute taxonomy — the single source for the normalizer and the `0005`/`0008` DB seed.

Mirrors `attributes-taxonomy.md` (authoritative). `is_art9` marks GDPR Art. 9 special categories
(birthplace ≈ ethnic origin) whose value + reasoning are encrypted at rest (rule 6).
`is_sensitive_tier` is our stricter-than-legal policy tier (encrypt + explicit acknowledgment).
`severity` is the per-persona risk matrix (at-risk vs job-seeker) driving the dashboard lens —
severity comes from the taxonomy, never hardcoded in the UI. `allowed_values` constrains the
categorical attributes. Pure data — no IO.
"""

from dataclasses import dataclass
from typing import Literal

from app.domain.output_schema import AttributeCode, ValueType

SeverityLevel = Literal["low", "moderate", "high", "extreme"]


@dataclass(frozen=True)
class Severity:
    """The per-persona risk severity for one attribute (the dashboard-ordering lens)."""

    atrisk: SeverityLevel
    jobseeker: SeverityLevel


@dataclass(frozen=True)
class AttributeSpec:
    code: AttributeCode
    label: str
    value_type: ValueType
    match_method: str
    is_art9: bool
    is_sensitive_tier: bool
    severity: Severity
    allowed_values: tuple[str, ...] | None


_EDUCATION_LEVELS = (
    "none",
    "high_school",
    "some_college",
    "associate",
    "bachelor",
    "master",
    "doctorate",
    "professional",
)
_RELATIONSHIP_VALUES = (
    "single",
    "in_relationship",
    "married",
    "divorced",
    "widowed",
    "complicated",
    "unknown",
)

# (severity matrix from attributes-taxonomy.md §"Persona severity matrix"; income's at-risk
# "low-moderate" is rounded up to moderate — a privacy tool should not under-report risk.)
ATTRIBUTES: tuple[AttributeSpec, ...] = (
    AttributeSpec("age", "Age", "numeric", "band", False, False, Severity("low", "low"), None),
    AttributeSpec(
        "sex",
        "Sex",
        "categorical",
        "exact",
        False,
        True,
        Severity("low", "low"),
        ("male", "female", "non-binary", "other", "unknown"),
    ),
    AttributeSpec(
        "location",
        "Location",
        "geo_hier",
        "judge",
        False,
        False,
        Severity("extreme", "moderate"),
        None,
    ),
    AttributeSpec(
        "birthplace",
        "Birthplace",
        "geo_hier",
        "judge",
        True,
        True,
        Severity("moderate", "low"),
        None,
    ),
    AttributeSpec(
        "occupation",
        "Occupation",
        "freetext_semantic",
        "judge",
        False,
        False,
        Severity("moderate", "high"),
        None,
    ),
    AttributeSpec(
        "education",
        "Education",
        "categorical",
        "exact_ordinal",
        False,
        False,
        Severity("low", "moderate"),
        _EDUCATION_LEVELS,
    ),
    AttributeSpec(
        "relationship",
        "Relationship",
        "categorical",
        "exact",
        False,
        True,
        Severity("moderate", "low"),
        _RELATIONSHIP_VALUES,
    ),
    AttributeSpec(
        "income", "Income", "numeric", "bracket", False, True, Severity("moderate", "low"), None
    ),
)

BY_CODE: dict[AttributeCode, AttributeSpec] = {spec.code: spec for spec in ATTRIBUTES}
