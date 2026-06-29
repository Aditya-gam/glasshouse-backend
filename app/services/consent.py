"""The consent gate (services-consent.md; CLAUDE.md rule 7; GDPR lawful basis).

No run starts without a valid, non-revoked `consents` row covering the subject + purpose. Deny by
default (security-privacy rule): missing or ambiguous consent → block. The gate lives in the service
layer so every caller — the API enqueue *and* the worker that executes the run — is gated; a worker
can never bypass it (workers.md). Art. 9 (special-category) inference, e.g. `birthplace`, needs its
own explicit consent, checked separately.
"""

from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories import consents as consents_repo

Purpose = Literal["self_audit", "art9_inference", "decoy"]


class ConsentRequiredError(Exception):
    """The consent gate denied a run; mapped to 403 problem+json at the API edge."""

    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        super().__init__(f"no active consent for '{purpose}'")


async def require_consent(conn: AsyncConnection, purpose: Purpose) -> None:
    """Block (raise) unless the caller holds a valid, non-revoked consent for `purpose`."""
    if not await consents_repo.has_active_consent(conn, purpose):
        raise ConsentRequiredError(purpose)


async def has_special_category_consent(conn: AsyncConnection) -> bool:
    """Whether Art. 9 inference is permitted — gates the birthplace attribute in the attack."""
    return await consents_repo.has_active_consent(conn, "art9_inference")
