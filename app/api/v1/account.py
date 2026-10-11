"""Account endpoints — data-subject rights + consent.

Contract-first stubs (write-paths WC): the final DTOs + route signatures are frozen here so the
frontend can generate its client; the services land in W1 (consent + account read), W3 (erasure),
W4 (retention), W5 (export). Consent grant/revoke writes an audit event (W1); erasure is synchronous
and returns 204 (write-paths D5); export is a run with an owner-only expiring download (D2/W5).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.api.errors import NotImplementedYet
from app.api.v1.schemas import (
    AccountRead,
    ConsentGrant,
    ConsentPurpose,
    Problem,
    RetentionUpdate,
    RunAccepted,
)

router = APIRouter(prefix="/v1/account", tags=["account"])

_NOT_YET = "account write paths land in M5.W (W1/W3/W4/W5)"
# A grant whose policy_version is not the current one is refused (W1); documented on the route now.
_PROBLEM = {"model": Problem, "content": {"application/problem+json": {}}}


@router.get("")
async def get_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> AccountRead:
    """Consents (one state per purpose) + retention setting + linked accounts."""
    raise NotImplementedYet(_NOT_YET)


@router.post("/consents", responses={status.HTTP_409_CONFLICT: _PROBLEM})
async def grant_consent(
    body: ConsentGrant, user_id: Annotated[UUID, Depends(get_current_user)]
) -> AccountRead:
    """Grant one consent purpose for the current policy version; returns the updated account.

    409 if `body.policy_version` is not the account's `current_policy_version` (a grant made against
    a stale screen), so the consent records the exact text the user agreed to. Audit event (W1).
    """
    raise NotImplementedYet(_NOT_YET)


@router.delete("/consents/{purpose}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_consent(
    purpose: ConsentPurpose, user_id: Annotated[UUID, Depends(get_current_user)]
) -> None:
    """Revoke one consent purpose (immediate; stops processing, does not delete data)."""
    raise NotImplementedYet(_NOT_YET)


@router.put("/retention")
async def set_retention(
    body: RetentionUpdate, user_id: Annotated[UUID, Depends(get_current_user)]
) -> AccountRead:
    """Set the retention window (`retain` | `discard`); returns the updated account."""
    raise NotImplementedYet(_NOT_YET)


@router.post("/export", status_code=status.HTTP_202_ACCEPTED)
async def export_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> RunAccepted:
    """DSAR export → an `export` run (202 + run_id); poll it, then GET the download below."""
    raise NotImplementedYet(_NOT_YET)


@router.get(
    "/export/{run_id}",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "the encrypted export bundle (owner only; expires)",
            "content": {"application/octet-stream": {}},
        }
    },
)
async def download_export(
    run_id: UUID, user_id: Annotated[UUID, Depends(get_current_user)]
) -> StreamingResponse:
    """Owner-only, expiring download of a finished export run's bundle; 404 otherwise."""
    raise NotImplementedYet(_NOT_YET)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def erase_account(user_id: Annotated[UUID, Depends(get_current_user)]) -> None:
    """Erasure — synchronous crypto-shred + cascade + object deletion; 204 when done (D5).

    The user id comes from the verified token only. The frontend deletes the Clerk identity after.
    """
    raise NotImplementedYet(_NOT_YET)
