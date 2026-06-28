"""Webhooks — inbound only, public (Svix signature-verified). Clerk → users mirror (M0.9)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import get_owner_engine
from app.services.webhooks import WebhookError, handle_clerk_event, verify_clerk_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/clerk", status_code=status.HTTP_204_NO_CONTENT)
async def clerk_webhook(
    request: Request,
    engine: Annotated[AsyncEngine, Depends(get_owner_engine)],
) -> Response:
    """Svix-verified; syncs user.created/updated/deleted into the users mirror. Idempotent."""
    payload = await request.body()
    try:
        event = verify_clerk_webhook(payload, dict(request.headers))
    except WebhookError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature"
        ) from None
    await handle_clerk_event(engine, event)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
