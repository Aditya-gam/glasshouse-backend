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


class DecoyNotConfirmedError(Exception):
    """A decoy edit was requested without the mandatory per-use confirmation; mapped to 403.

    Distinct from a missing standing consent: even with `decoy` consent on file, each decoy edit
    needs an explicit per-use confirm so a falsehood is never auto-injected (text-remediation §2).
    """

    def __init__(self) -> None:
        super().__init__("decoy edit requires explicit per-use confirmation")


async def require_consent(conn: AsyncConnection, purpose: Purpose) -> None:
    """Block (raise) unless the caller holds a valid, non-revoked consent for `purpose`."""
    if not await consents_repo.has_active_consent(conn, purpose):
        raise ConsentRequiredError(purpose)


async def require_decoy(conn: AsyncConnection, *, confirmed: bool) -> None:
    """Fail closed unless BOTH decoy gates hold: the standing consent AND a per-use confirm.

    Two independent factors (defense-in-depth, text-remediation.md §2): the global `decoy` opt-in
    proves the user enabled decoy mode at all; the per-use `confirmed` proves they affirmed THIS
    specific falsehood — a decoy is never auto-injected (off by default). Deny on either. The
    per-use confirm is checked first (cheap, in-memory) before the DB round-trip for consent.
    """
    if not confirmed:
        raise DecoyNotConfirmedError()
    await require_consent(conn, "decoy")


async def has_special_category_consent(conn: AsyncConnection) -> bool:
    """Whether Art. 9 inference is permitted — gates the birthplace attribute in the attack."""
    return await consents_repo.has_active_consent(conn, "art9_inference")
