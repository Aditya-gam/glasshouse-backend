"""Profiler emission schema — `RawAttributeGuess` (output-schema.md §9/§10).

The shallow, permissive shape `instructor` validates straight from the model. The typed,
taxonomy-pinned canonical `AttributeGuess` is built downstream (normalizer + self-consistency,
M1.7); this is only the emission layer, kept deliberately shallow for local-model reliability.
Pure data — no IO.
"""

from typing import Literal

from pydantic import BaseModel, Field

AttributeCode = Literal[
    "age",
    "sex",
    "location",
    "birthplace",
    "occupation",
    "education",
    "relationship",
    "income",
]


class RawEvidence(BaseModel):
    """A model-asserted citation. `ref_type`/`modality` are resolved downstream, not by the model.

    (`region` for image evidence is added with the image pipeline, M4.)
    """

    ref_id: str
    quote: str | None = None
    rationale: str | None = None


class RawCandidate(BaseModel):
    """One free-text guess plus the model's own confidence; structured/normalized downstream."""

    value_text: str
    self_confidence: float = Field(ge=0, le=1)
    evidence: list[RawEvidence] = Field(default_factory=list)


class RawAttributeGuess(BaseModel):
    """One attribute attempt; `candidates` is empty iff abstained, else ranked best-first."""

    attribute: AttributeCode
    status: Literal["inferred", "abstained"]
    candidates: list[RawCandidate] = Field(default_factory=list, max_length=3)
    reasoning: str | None = None
