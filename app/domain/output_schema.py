"""Profiler output contract — emission + canonical layers (output-schema.md).

Two layers (§1): the LLM emits the shallow, permissive ``RawAttributeGuess`` (what ``instructor``
validates); deterministic Tier-1 normalizers turn it into the typed ``AttributeGuess`` that measure,
the DB, and the frontend consume. Pure data — no IO.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

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
ValueType = Literal["numeric", "categorical", "geo_hier", "freetext_semantic"]
Modality = Literal["text", "image", "multimodal"]

# attribute → its canonical value_type (the §10 validator map; the DB attributes table is source).
_VALUE_TYPE: dict[AttributeCode, ValueType] = {
    "age": "numeric",
    "income": "numeric",
    "sex": "categorical",
    "education": "categorical",
    "relationship": "categorical",
    "location": "geo_hier",
    "birthplace": "geo_hier",
    "occupation": "freetext_semantic",
}


# ----------------------------------------------------------- emission layer (§9) --
class RawEvidence(BaseModel):
    """A model-asserted citation; `ref_type`/`modality` are resolved downstream."""

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


class RawProfilerOutput(BaseModel):
    """The joint pass emission envelope — up to 8 guesses, one per attribute attempted."""

    guesses: list[RawAttributeGuess] = Field(default_factory=list, max_length=8)


# --------------------------------------------------------- canonical values (§5) --
class Range(BaseModel):
    low: float
    high: float


class NumericValue(BaseModel):
    value_type: Literal["numeric"] = "numeric"
    estimate: float
    range: Range | None = None
    bracket: Literal["low", "medium", "high"] | None = None
    unit: str | None = None


class CategoricalValue(BaseModel):
    value_type: Literal["categorical"] = "categorical"
    value: str  # validated against attributes.allowed_values at the service/normalizer layer


class GeoHierValue(BaseModel):
    value_type: Literal["geo_hier"] = "geo_hier"
    country: str | None = None
    region: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    precision_level: Literal["country", "region", "city", "neighborhood"]
    geonames_id: int | None = None  # resolved by the GeoNames normalizer (M1.7b)


class FreeTextValue(BaseModel):
    value_type: Literal["freetext_semantic"] = "freetext_semantic"
    text: str
    normalized_label: str | None = None


AttributeValue = Annotated[
    NumericValue | CategoricalValue | GeoHierValue | FreeTextValue,
    Field(discriminator="value_type"),
]


# ------------------------------------------------------ canonical guess (§3-§8) --
class Agreement(BaseModel):
    n_runs: int
    n_agree: int
    fraction: float


class Confidence(BaseModel):
    """Only `raw` feeds calibration; never shown to the user bare (overview.md separation rule)."""

    raw: float = Field(ge=0, le=1)
    source: Literal["self_reported", "self_consistency"]
    self_reported: float | None = Field(default=None, ge=0, le=1)
    agreement: Agreement | None = None


class Span(BaseModel):
    quote: str
    start: int | None = None
    end: int | None = None


class Evidence(BaseModel):
    ref_type: Literal["item", "media_asset", "exif_finding"]
    ref_id: str
    modality: Literal["text", "image"]
    span: Span | None = None
    rationale: str | None = None
    proxy_score: float | None = None
    citation_frequency: float | None = None
    marginal_effect: float | None = None  # filled by defend/attribution-ablation (M3)


class Candidate(BaseModel):
    rank: int = Field(ge=1, le=3)
    value: AttributeValue
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)


class AttributeGuess(BaseModel):
    """The canonical per-attribute object measure/DB/frontend consume (one per `inferences` row)."""

    attribute: AttributeCode
    modality: Modality
    status: Literal["inferred", "abstained"]
    candidates: list[Candidate] = Field(default_factory=list, max_length=3)
    reasoning: str | None = None
    reasoning_reveals_art9: bool = False

    @model_validator(mode="after")
    def _consistency(self) -> "AttributeGuess":
        if self.status == "abstained" and self.candidates:
            raise ValueError("an abstained guess must have no candidates")
        expected = _VALUE_TYPE[self.attribute]
        for candidate in self.candidates:
            if candidate.value.value_type != expected:
                raise ValueError(f"{self.attribute} requires value_type={expected}")
        return self
