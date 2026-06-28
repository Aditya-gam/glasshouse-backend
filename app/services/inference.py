"""Attack service — retrieve → infer → normalize → persist.

`run_text_attack` is the M1.7 joint pass (the path M1.9's arq worker will call): the Retriever's
evidence set → one profiler call inferring all 8 attributes → the normalizer → persisted canonical
inferences. The legacy single-attribute `run_attack` (tracer, T4) stays until M1.9 rewires the
endpoint + retires the tracer schema. Content is decrypted in memory only and never logged.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.attributes import BY_CODE
from app.domain.normalize import normalize_guess
from app.domain.output_schema import AttributeCode, AttributeGuess
from app.gateway.client import GatewayClient, Profiler
from app.gateway.prompts import ENGINE_VERSION, build_user_prompt
from app.repositories import inferences as inferences_repo
from app.repositories import items as items_repo
from app.repositories import profiles as profiles_repo
from app.repositories import runs as runs_repo
from app.retrieval.embedder import Embedder
from app.retrieval.pii import PiiDetector
from app.retrieval.retriever import retrieve_evidence

# The (model + prompt) pin for the tracer path; the real engine_version is ENGINE_VERSION (M1.7).
_TRACER_ENGINE_VERSION = "tracer-profiler@qwen2.5"

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
    """Tracer single-attribute pass (T4) — retained until M1.9 rewires the endpoint."""
    run_id = await runs_repo.create_run(
        conn,
        owner_user_id,
        run_type="attack",
        status="running",
        engine_version=_TRACER_ENGINE_VERSION,
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


# --- M1.7 joint attack -----------------------------------------------------------------------


def _valid_ref(ref_id: str, valid_item_ids: set[UUID]) -> UUID | None:
    """Resolve a model-cited ref_id to a real item id, or None (drops fabricated references)."""
    try:
        candidate = UUID(ref_id)
    except ValueError:
        return None
    return candidate if candidate in valid_item_ids else None


async def persist_attribute_guess(
    conn: AsyncConnection,
    guess: AttributeGuess,
    *,
    valid_item_ids: set[UUID],
    owner_user_id: UUID,
    profile_id: UUID,
    run_id: UUID,
    master_key: str,
) -> None:
    """Persist one canonical guess (inference + candidates + evidence); Art. 9 values encrypted."""
    is_art9 = BY_CODE[guess.attribute].is_art9
    inference_id = await inferences_repo.insert_inference_v2(
        conn,
        run_id=run_id,
        profile_id=profile_id,
        owner_user_id=owner_user_id,
        attribute_code=guess.attribute,
        status=guess.status,
        engine_version=ENGINE_VERSION,
        reasoning=guess.reasoning,
        reasoning_reveals_art9=guess.reasoning_reveals_art9,
        master_key=master_key,
    )
    for candidate in guess.candidates:
        candidate_id = await inferences_repo.insert_candidate(
            conn,
            inference_id=inference_id,
            rank=candidate.rank,
            value_json=candidate.value.model_dump_json(),
            raw_confidence=candidate.confidence.raw,
            confidence_source=candidate.confidence.source,
            owner_user_id=owner_user_id,
            is_art9=is_art9,
            master_key=master_key,
        )
        for evidence in candidate.evidence:
            ref_uuid = _valid_ref(evidence.ref_id, valid_item_ids)
            if ref_uuid is None:
                continue  # drop fabricated/invalid reference (anti-hallucination, output-schema §6)
            await inferences_repo.insert_evidence(
                conn,
                candidate_id=candidate_id,
                ref_type=evidence.ref_type,
                ref_id=ref_uuid,
                span_json=evidence.span.model_dump_json() if evidence.span else None,
                rationale=evidence.rationale,
                owner_user_id=owner_user_id,
                master_key=master_key,
            )


async def run_text_attack(
    conn: AsyncConnection,
    gateway: Profiler,
    embedder: Embedder,
    pii_detector: PiiDetector,
    *,
    owner_user_id: UUID,
    master_key: str,
) -> UUID:
    """Joint text attack (M1.7): retrieve → infer all 8 → normalize → persist. RLS-scoped."""
    profile_id = await profiles_repo.get_or_create_self_profile(conn, owner_user_id)
    run_id = await runs_repo.insert_run_v2(
        conn, profile_id, run_type="attack", status="running", engine_version=ENGINE_VERSION
    )
    evidence = await retrieve_evidence(conn, embedder, pii_detector, master_key=master_key)
    valid_item_ids = {item.id for item in evidence}
    content = build_user_prompt([(str(item.id), item.text) for item in evidence])
    for raw in await gateway.profile_all(content=content):
        guess = normalize_guess(raw)
        await persist_attribute_guess(
            conn,
            guess,
            valid_item_ids=valid_item_ids,
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            run_id=run_id,
            master_key=master_key,
        )
    await runs_repo.set_run_status(conn, run_id, "succeeded", finished=True)
    return run_id
