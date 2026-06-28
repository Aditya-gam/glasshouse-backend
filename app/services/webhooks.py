"""Clerk webhook handling — Svix-verified, idempotent user sync (webhooks.md).

Verify the Svix signature before processing, then mirror user.created/updated/deleted into the
users table (user.deleted cascades to the DEK = crypto-shred). Membership/org sync is a follow-up.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from svix.webhooks import Webhook, WebhookVerificationError

from app.core.config import get_auth_settings
from app.repositories.users import delete_user_by_clerk_id, upsert_user


class WebhookError(Exception):
    """Invalid/unverifiable webhook — mapped to 400 at the edge."""


def verify_clerk_webhook(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Verify the Svix signature and return the parsed event; raise WebhookError if invalid."""
    secret = get_auth_settings().clerk_webhook_secret
    if not secret:
        raise WebhookError("Clerk webhook secret is not configured")
    try:
        event: dict[str, Any] = Webhook(secret).verify(payload, headers)
    except (WebhookVerificationError, ValueError, KeyError) as exc:
        # Wrong signature, malformed base64, or missing headers — all reject as unverified.
        raise WebhookError(str(exc)) from exc
    return event


def _primary_email(data: dict[str, Any]) -> str | None:
    emails = data.get("email_addresses") or []
    if not emails:
        return None
    primary_id = data.get("primary_email_address_id")
    for entry in emails:
        if entry.get("id") == primary_id:
            email: str | None = entry.get("email_address")
            return email
    first: str | None = emails[0].get("email_address")
    return first


async def handle_clerk_event(engine: AsyncEngine, event: dict[str, Any]) -> None:
    """Apply a verified Clerk event to the users mirror (idempotent)."""
    event_type = event.get("type")
    data = event.get("data") or {}
    clerk_user_id = data.get("id")
    if not clerk_user_id:
        return
    if event_type in {"user.created", "user.updated"}:
        async with engine.begin() as conn:
            await upsert_user(conn, clerk_user_id, _primary_email(data))
    elif event_type == "user.deleted":
        async with engine.begin() as conn:
            await delete_user_by_clerk_id(conn, clerk_user_id)
