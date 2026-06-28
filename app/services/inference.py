"""Attack service — orchestrates retrieve → infer → persist.

The same code path M1.9's arq worker will call. Content is decrypted in memory only and
never logged. (Consent gating is M1.10; the queue + self-consistency + normalizer are M1.6–M1.9.)
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.output_schema import AttributeCode
from app.gateway.client import GatewayClient
from app.repositories import inferences as inferences_repo
from app.repositories import items as items_repo
from app.repositories import runs as runs_repo

# The (model + prompt) pin for the calibration map; the real engine_version lands at M1.5/M1.7.
_ENGINE_VERSION = "tracer-profiler@qwen2.5"

_PROFILER_PROMPT = (
    "You are a privacy auditor analysing a person's own public posts. "
    "Infer their single most likely {attribute}. Return candidates "
    "(value_text, self_confidence 0-1, evidence) best-first, or status=abstained "
    "with no candidates if there is no signal."
)


async def run_attack(
    conn: AsyncConnection,
    gateway: GatewayClient,
    *,
    owner_user_id: UUID,
    attribute: AttributeCode,
    master_key: str,
    idempotency_key: str | None = None,
) -> UUID:
    """Run one synchronous attack pass, persist the run + inference, and return the run id."""
    run_id = await runs_repo.create_run(
        conn,
        owner_user_id,
        run_type="attack",
        status="running",
        engine_version=_ENGINE_VERSION,
        idempotency_key=idempotency_key,
    )

    texts = await items_repo.get_items_text(conn, master_key)
    guess = await gateway.profile_attribute(
        system_prompt=_PROFILER_PROMPT.format(attribute=attribute),
        content="\n\n".join(texts),
    )
    top_value = guess.candidates[0].value_text if guess.candidates else None

    await inferences_repo.insert_inference(
        conn,
        owner_user_id,
        guess.attribute,
        guess.reasoning or "",
        master_key,
        run_id=run_id,
        top_value_text=top_value,
        status=guess.status,
    )
    await runs_repo.set_run_status(conn, run_id, "succeeded", finished=True)
    return run_id
