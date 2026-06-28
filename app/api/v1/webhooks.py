"""Webhooks — inbound only, public (signature-verified). Clerk sync lands at M0.9."""

from fastapi import APIRouter, Request, status

from app.api.errors import NotImplementedYet

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/clerk", status_code=status.HTTP_204_NO_CONTENT)
async def clerk_webhook(request: Request) -> None:
    """Svix signature-verified; handles user.created/deleted + org/membership sync. Idempotent."""
    raise NotImplementedYet("Clerk webhook lands at M0.9")
