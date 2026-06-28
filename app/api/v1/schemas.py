"""API DTOs for the runs endpoints (tracer subset).

The full per-operation DTOs + problem+json envelope are frozen at M5.1 (06-api/schemas.md);
this is the minimal shape the tracer needs. `params` is flattened to `attribute` for now.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.domain.output_schema import AttributeCode


class RunCreate(BaseModel):
    """POST body. `type` is attack-only for the tracer (eval/remediation arrive later)."""

    type: Literal["attack"] = "attack"
    attribute: AttributeCode = "location"
    idempotency_key: str | None = None


class RunAccepted(BaseModel):
    """202 response — the client polls GET /v1/runs/{id} until terminal."""

    run_id: UUID
    status: str


class InferenceRead(BaseModel):
    attribute: str
    status: str
    top_value: str | None
    reasoning: str | None


class RunRead(BaseModel):
    run_id: UUID
    type: str
    status: str
    engine_version: str | None
    created_at: datetime
    finished_at: datetime | None
    inferences: list[InferenceRead]
