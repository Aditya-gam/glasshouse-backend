"""API DTOs — the wire contract (06-api/schemas.md; shapes frozen in prototype HANDOFF §3).

Per-operation Pydantic v2 models, **never** the ORM. Reliability is always the calibrated
point + interval (``{point, lo, hi}``); raw model confidence is never serialized. Field names
are snake_case; the frontend generates its typed client from the published OpenAPI.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

SeverityLevel = Literal["low", "moderate", "high", "extreme"]


class ApiModel(BaseModel):
    """Base DTO — standardizes serialization across the contract."""

    model_config = ConfigDict(populate_by_name=True)


# ------------------------------------------------------------------- core --
class Problem(ApiModel):
    """RFC 9457 problem+json error body (the one error shape across the API)."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class Reliability(ApiModel):
    """Calibrated reliability — point estimate + interval, 0..1 (the UI renders %)."""

    point: float
    lo: float
    hi: float


class Severity(ApiModel):
    """Per-persona severity matrix; the UI computes `balanced = max(atrisk, jobseeker)`."""

    atrisk: SeverityLevel
    jobseeker: SeverityLevel


# ------------------------------------------------------------------- runs --
class RunCreate(ApiModel):
    """POST /v1/runs body. `params` is type-specific.

    Retries are deduped via the `Idempotency-Key` request header (api-design rule), not a field.
    """

    type: Literal["attack", "eval", "remediation"]
    params: dict[str, object] = {}


class RunAccepted(ApiModel):
    """202 response — the client polls GET /v1/runs/{id} until terminal."""

    run_id: UUID
    status: str


class RunStatus(ApiModel):
    """Poll response for a run."""

    id: UUID
    type: str
    status: str
    engine_version: str | None = None
    error: str | None = None


# ------------------------------------------------------------- inferences --
class Candidate(ApiModel):
    rank: int
    label: str
    note: str


class BBox(ApiModel):
    x: float
    y: float
    w: float
    h: float


class Exif(ApiModel):
    gps: str
    place: str
    device: str
    taken: str


class EvidenceRead(ApiModel):
    """One piece of evidence — `proven` (causal ablation) or `likely` (correlational)."""

    id: str
    kind: Literal["proven", "likely"]
    type: Literal["text", "photo"]
    source: str
    date: str
    text: str | None = None
    spans: list[str] | None = None
    caption: str | None = None
    region: BBox | None = None
    exif: Exif | None = None
    rationale: str
    marginal: float | None = None  # proven: ablation Δ (negative %)
    proxy: float | None = None  # likely: proxy_score 0..100
    citation: float | None = None  # likely: citation_frequency 0..100


class AttributeRead(ApiModel):
    """A dashboard attribute card. `null` reliability ⇔ abstain."""

    code: str
    label: str
    value: str | None
    detail: str | None
    reliability: Reliability | None
    evidence: str
    evidence_count: int | None = None
    abstain: bool | None = None
    sensitive: bool | None = None
    art9: bool | None = None
    severity: Severity


class AttributeFindingRead(ApiModel):
    """Attribution detail — AttributeRead enriched with candidates + per-item evidence."""

    code: str
    label: str
    value: str | None
    detail: str | None = None
    reliability: Reliability | None
    severity: Severity
    sensitive: bool | None = None
    art9: bool | None = None
    precision: str | None = None
    neighborhood: str | None = None
    reasoning: str
    candidates: list[Candidate]
    text_only_reliability: Reliability | None = None
    evidence_items: list[EvidenceRead]


class InferenceConfirm(ApiModel):
    """POST /v1/inferences/{id}/confirm — the user confirms/denies (a live ground-truth signal)."""

    value: str
    confirmed: bool


# ------------------------------------------------------------ remediations --
class DiffSeg(ApiModel):
    """A diff segment; `insf` = an inserted falsehood (decoy)."""

    t: Literal["eq", "del", "ins", "insf"]
    v: str


class DefendEdit(ApiModel):
    src: str
    date: str
    segs: list[DiffSeg] | None = None
    remove: bool | None = None
    original: str | None = None
    exif: bool | None = None
    crop: bool | None = None
    decoy: bool | None = None
    note: str | None = None


class DefendTarget(ApiModel):
    attribute: str
    value: str
    before: Reliability


class DefendOptionRead(ApiModel):
    """One frontier option; `after` is proven by the held-out adversary (point + interval)."""

    key: Literal["minimal", "stronger", "remove", "decoy"]
    name: str
    desc: str
    truthful: bool
    recommended: bool | None = None
    opt_in: bool | None = None
    remove: bool | None = None
    after: Reliability
    recovered: bool
    misled: str | None = None  # decoy: the wrong value the adversary now guesses
    utility: int | None
    utility_label: str
    edits: list[DefendEdit]


class RemediationRead(ApiModel):
    """A proven, advise-only remediation; `within_noise`/`cant_break` are honest non-success."""

    status: Literal["proven", "within_noise", "cant_break"]
    target: DefendTarget
    options: list[DefendOptionRead]


class RemediationCreate(ApiModel):
    """POST /v1/inferences/{id}/remediations — decoy requires per-use consent."""

    strategy: str | None = None
    decoy: bool = False


# ------------------------------------------------- imports / connectors --
class ImportRead(ApiModel):
    id: UUID
    platform: str | None = None
    status: str
    kept: int | None = None
    dropped: int | None = None
    created_at: datetime | None = None


class ConnectedAccountRead(ApiModel):
    """Linked account — status + handle only, never tokens."""

    id: UUID
    platform: str
    handle: str | None = None
    status: str


class ConnectorCreate(ApiModel):
    platform: Literal["reddit", "mastodon", "x"]


class ConnectorStart(ApiModel):
    authorize_url: str


# ------------------------------------------------------------- account --
class ConsentRead(ApiModel):
    purpose: bool
    art9: bool
    decoy: bool


class ConsentUpdate(ApiModel):
    purpose: str
    special_category: bool = False
    decoy: bool = False


class RetentionUpdate(ApiModel):
    retention: Literal["retain", "discard"]


class AccountRead(ApiModel):
    consents: ConsentRead
    retention: Literal["retain", "discard"]
    connected_accounts: list[ConnectedAccountRead]


# --------------------------------------------------------------- eval --
class BenchRow(ApiModel):
    label: str
    top1: float
    top3: float


class BenchmarkRead(ApiModel):
    rows: list[BenchRow]
    calibration: list[tuple[float, float]]  # [predicted, empirical]


class EvalResultRead(ApiModel):
    attribute: str
    modality: str
    top1: float
    top3: float
    engine_version: str
