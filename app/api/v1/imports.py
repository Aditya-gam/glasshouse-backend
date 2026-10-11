"""Imports endpoints — contract-first stubs (write-paths WC); ingestion lands in W2.

`POST /v1/imports` is multipart: a `platform` plus the files the adapter needs (the browser extracts
`comments.csv`/`posts.csv` or `data/tweets.js` before upload), or a zip under the cap. It creates an
`ingestion` run (202 + run_id) on the existing poll/SSE; kept/dropped counts land on `ImportRead`.
Consent-gated; dedupes on `Idempotency-Key` like `POST /v1/runs`.
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile, status

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import ImportPage, ImportRead, RunAccepted

router = APIRouter(prefix="/v1/imports", tags=["imports"])

_NOT_YET = "ingestion lands in M5.W (W2)"


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    user_id: Annotated[UUID, Depends(get_current_user)],
    platform: Annotated[Literal["reddit", "x"], Form()],
    files: Annotated[list[UploadFile], File()],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> RunAccepted:
    """Upload an export → an ingestion run (202 + run_id). Consent-gated; limits enforced in W2."""
    raise NotImplementedYet(_NOT_YET)


@router.get("")
async def list_imports(user_id: Annotated[UUID, Depends(get_current_user)]) -> ImportPage:
    """The caller's imports, newest first, cursor-paginated (envelope, not a bare array)."""
    raise NotImplementedYet(_NOT_YET)


@router.get("/{import_id}")
async def get_import(
    import_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> ImportRead:
    raise NotImplementedYet(_NOT_YET)
