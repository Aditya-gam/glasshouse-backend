"""The 8-attribute taxonomy — the single source for the normalizer and the `0005` DB seed.

Mirrors `attributes-taxonomy.md` (authoritative). `is_art9` marks GDPR Art. 9 special categories
(birthplace ≈ ethnic origin) whose value + reasoning are encrypted at rest (rule 6).
`allowed_values` constrains the categorical attributes. Pure data — no IO.
"""

from dataclasses import dataclass

from app.domain.output_schema import AttributeCode, ValueType


@dataclass(frozen=True)
class AttributeSpec:
    code: AttributeCode
    label: str
    value_type: ValueType
    match_method: str
    is_art9: bool
    allowed_values: tuple[str, ...] | None


ATTRIBUTES: tuple[AttributeSpec, ...] = (
    AttributeSpec("age", "Age", "numeric", "band", False, None),
    AttributeSpec(
        "sex",
        "Sex",
        "categorical",
        "exact",
        False,
        ("male", "female", "non-binary", "other", "unknown"),
    ),
    AttributeSpec("location", "Location", "geo_hier", "judge", False, None),
    AttributeSpec("birthplace", "Birthplace", "geo_hier", "judge", True, None),
    AttributeSpec("occupation", "Occupation", "freetext_semantic", "judge", False, None),
    AttributeSpec(
        "education",
        "Education",
        "categorical",
        "exact_ordinal",
        False,
        (
            "none",
            "high_school",
            "some_college",
            "associate",
            "bachelor",
            "master",
            "doctorate",
            "professional",
        ),
    ),
    AttributeSpec(
        "relationship",
        "Relationship",
        "categorical",
        "exact",
        False,
        ("single", "in_relationship", "married", "divorced", "widowed", "complicated", "unknown"),
    ),
    AttributeSpec("income", "Income", "numeric", "bracket", False, None),
)

BY_CODE: dict[AttributeCode, AttributeSpec] = {spec.code: spec for spec in ATTRIBUTES}
