"""The `attack` arq task → `services/inference` (workers.md: thin wrapper, no logic in workers).

Opens an RLS-scoped session on the app role, **re-checks consent** (revocation is immediate, so the
gate runs at execution too, not only at enqueue), builds the real dependencies, and executes the
pre-created run. Secrets never travel through the queue — the worker resolves the master key locally
(env/KMS). On failure the run is marked terminal; non-consent failures re-raise for arq retry/DLQ.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import crypto
from app.db.rls import set_rls_context
from app.db.session import app_engine
from app.gateway.client import GatewayClient
from app.repositories import runs as runs_repo
from app.retrieval.embedder import default_embedder
from app.retrieval.pii import default_pii_detector
from app.services.consent import (
    ConsentRequiredError,
    has_special_category_consent,
    require_consent,
)
from app.services.geocoding import default_geocoder
from app.services.inference import execute_attack_run
from app.services.occupation import GatewayOccupationJudge

logger = logging.getLogger(__name__)


async def attack_run(ctx: dict[Any, Any], run_id: str, owner_user_id: str) -> None:
    """Execute a queued attack run; `ctx` is arq's job context. No content/secrets are logged."""
    run_uuid, owner_uuid = UUID(run_id), UUID(owner_user_id)
    master_key = crypto.get_master_key()  # resolved locally — never carried in the job payload
    gateway = GatewayClient()
    async with app_engine.connect() as conn:
        try:
            async with conn.begin():
                await set_rls_context(conn, owner_uuid)
                await require_consent(conn, "self_audit")  # revocation is immediate → re-check here
                allow_art9 = await has_special_category_consent(conn)
                await execute_attack_run(
                    conn,
                    run_uuid,
                    gateway,
                    default_embedder(),
                    default_pii_detector(),
                    default_geocoder(),
                    owner_user_id=owner_uuid,
                    master_key=master_key,
                    allow_special_category=allow_art9,
                    judge=GatewayOccupationJudge(gateway),
                )
        except ConsentRequiredError:
            logger.warning("attack run %s blocked: consent revoked", run_id)
            await _mark_failed(conn, owner_uuid, run_uuid)  # terminal, not retried
        except Exception as exc:
            logger.error(
                "attack run %s failed: %s", run_id, type(exc).__name__
            )  # type only, no PII
            await _mark_failed(conn, owner_uuid, run_uuid)
            raise  # let arq retry with backoff, then dead-letter


async def _mark_failed(conn: AsyncConnection, owner_uuid: UUID, run_uuid: UUID) -> None:
    """Mark the run failed in a fresh transaction (the work transaction has rolled back)."""
    async with conn.begin():
        await set_rls_context(conn, owner_uuid)
        await runs_repo.set_run_status(conn, run_uuid, "failed", finished=True)
