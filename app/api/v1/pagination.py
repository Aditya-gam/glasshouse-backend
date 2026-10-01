"""Opaque cursor codec for keyset pagination (api-design: an opaque cursor off a stable key).

A cursor is a base64url-encoded `(created_at, id)` key — the stable sort key of a keyset page. It
is opaque to the client: they echo a prior page's `next_cursor` verbatim, never construct one. A
tampered or stale cursor raises `InvalidCursor`, mapped to 422 at the API edge. Pure (no IO).
"""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID


class InvalidCursor(ValueError):
    """A client-supplied pagination cursor was malformed; mapped to 422 at the edge."""


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    """The opaque cursor for the stable `(created_at, id)` key — the client treats it as opaque."""
    payload = json.dumps({"ts": created_at.isoformat(), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode an opaque cursor back to its `(created_at, id)` key; InvalidCursor if malformed."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(payload["ts"]), UUID(payload["id"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("malformed pagination cursor") from exc
