"""Clerk session JWT verification (JWKS) — auth-clerk.md.

Caches the JWKS per URL; verifies the RS256 signature + exp/sub (+ iss/aud when configured) and
returns the claims. Resolution of the internal users.id and the current-user dependency live in
api/deps.py. API-key auth is not used for user requests (Clerk OAuth only).
"""

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError, PyJWTError

from app.core.config import get_auth_settings


class AuthError(Exception):
    """Invalid/expired/unverifiable token — mapped to 401 at the edge."""


@dataclass(frozen=True)
class ClerkClaims:
    clerk_user_id: str
    org_id: str | None


_jwks_clients: dict[str, PyJWKClient] = {}


def _jwks_client(url: str) -> PyJWKClient:
    """A JWKS client per URL; PyJWKClient caches the fetched keys internally."""
    client = _jwks_clients.get(url)
    if client is None:
        client = PyJWKClient(url)
        _jwks_clients[url] = client
    return client


def verify_token(token: str) -> ClerkClaims:
    """Verify a Clerk session JWT; return its claims or raise AuthError."""
    settings = get_auth_settings()
    if not settings.clerk_jwks_url:
        raise AuthError("Clerk auth is not configured")
    try:
        signing_key = _jwks_client(settings.clerk_jwks_url).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.clerk_audience,
            issuer=settings.clerk_issuer,
            options={"require": ["exp", "sub"], "verify_aud": bool(settings.clerk_audience)},
        )
    except (PyJWTError, PyJWKClientError) as exc:
        raise AuthError(str(exc)) from exc
    return ClerkClaims(clerk_user_id=payload["sub"], org_id=payload.get("org_id"))
