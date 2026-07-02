"""Attack service — retrieve → infer → normalize → persist.

The joint pass: the Retriever's evidence set → an N-run Profiler ensemble inferring all 8 attributes
→ the normalizer → self-consistency clustering → persisted canonical inferences + `run_metrics`.
`create_run` (the endpoint) enqueues a queued run; the arq worker then calls `execute_attack_run`.
Content is decrypted in memory only and never logged.
"""

import time
from collections import defaultdict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_attack_settings
from app.domain.attributes import BY_CODE
from app.domain.consistency import aggregate
from app.domain.normalize import normalize_guess
from app.domain.output_schema import AttributeCode, AttributeGuess
from app.gateway.client import Profiler
from app.gateway.prompts import ENGINE_VERSION, build_user_prompt
from app.repositories import inferences as inferences_repo
from app.repositories import profiles as profiles_repo
from app.repositories import run_metrics as run_metrics_repo
from app.repositories import runs as runs_repo
from app.retrieval.embedder import Embedder
from app.retrieval.pii import PiiDetector
from app.retrieval.retriever import retrieve_evidence
from app.services.geocoding import Geocoder, enrich_geo
from app.services.occupation import OccupationJudge, StringMatchJudge, aggregate_occupation

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


async def execute_attack_run(
    conn: AsyncConnection,
    run_id: UUID,
    gateway: Profiler,
    embedder: Embedder,
    pii_detector: PiiDetector,
    geocoder: Geocoder,
    *,
    owner_user_id: UUID,
    master_key: str,
    allow_special_category: bool,
    n_runs: int | None = None,
    temperature: float | None = None,
    judge: OccupationJudge | None = None,
) -> None:
    """Execute a pre-created run (M1.9 worker core): running → retrieve → N runs (temp>0) →
    normalize → geocode → cluster by meaning → persist the consensus + run_metrics → succeeded.

    Art. 9 attributes (birthplace) are inferred only with explicit special-category consent
    (services-consent.md). RLS-scoped; content decrypted in memory, never logged.
    """
    occupation_judge = judge or StringMatchJudge()
    settings = get_attack_settings()
    runs = n_runs if n_runs is not None else settings.n_runs
    # N=1 dev/fast is deterministic (temp 0); the ensemble samples at temp>0 so runs can differ.
    temp = (
        0.0
        if runs < 2
        else (temperature if temperature is not None else settings.sampling_temperature)
    )
    if not await runs_repo.set_run_status_where(conn, run_id, "running", allowed_from=("queued",)):
        return  # not claimable (canceled before pickup, or already running) — do nothing
    started = time.monotonic()
    run = await runs_repo.get_run(conn, run_id)
    if run is None:  # claimed above, so only RLS mid-flight revocation can land here
        return
    # the run's own profile — not the self profile — so eval runs on benchmark profiles work too.
    profile_id = run.profile_id
    evidence = await retrieve_evidence(
        conn, embedder, pii_detector, profile_id=profile_id, master_key=master_key
    )
    valid_item_ids = {item.id for item in evidence}
    content = build_user_prompt([(str(item.id), item.text) for item in evidence])

    by_attribute: dict[AttributeCode, list[AttributeGuess]] = defaultdict(list)
    for _ in range(runs):
        seen: set[AttributeCode] = set()
        for raw in await gateway.profile_all(content=content, temperature=temp):
            if raw.attribute in seen:  # one guess per attribute per run → the denominator stays N
                continue
            seen.add(raw.attribute)
            by_attribute[raw.attribute].append(await enrich_geo(normalize_guess(raw), geocoder))

    for attribute, guesses in by_attribute.items():
        if BY_CODE[attribute].is_art9 and not allow_special_category:
            continue  # Art. 9 (birthplace) needs explicit special-category consent — skip
        if runs < 2:
            consensus = guesses[0]  # dev/fast: the single self_reported pass, no clustering
        elif attribute == "occupation":
            consensus = await aggregate_occupation(guesses, occupation_judge, n_runs=runs)
        else:
            consensus = aggregate(attribute, guesses, n_runs=runs)
        await persist_attribute_guess(
            conn,
            consensus,
            valid_item_ids=valid_item_ids,
            owner_user_id=owner_user_id,
            profile_id=profile_id,
            run_id=run_id,
            master_key=master_key,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    await run_metrics_repo.insert_run_metrics(
        conn, run_id=run_id, latency_ms=latency_ms, model_calls=runs
    )
    # succeeded only from running — a cancel that landed mid-run keeps the run canceled.
    await runs_repo.set_run_status_where(
        conn, run_id, "succeeded", allowed_from=("running",), finished=True
    )


async def run_text_attack(
    conn: AsyncConnection,
    gateway: Profiler,
    embedder: Embedder,
    pii_detector: PiiDetector,
    geocoder: Geocoder,
    *,
    owner_user_id: UUID,
    master_key: str,
    n_runs: int | None = None,
    temperature: float | None = None,
    judge: OccupationJudge | None = None,
) -> UUID:
    """Create a run and execute it inline — the direct entry (tests); the worker uses the two
    steps separately (`create_run` enqueues, then `execute_attack_run` runs). RLS-scoped.
    """
    profile_id = await profiles_repo.get_or_create_self_profile(conn, owner_user_id)
    run_id = await runs_repo.insert_run_v2(
        conn, profile_id, run_type="attack", status="queued", engine_version=ENGINE_VERSION
    )
    await execute_attack_run(
        conn,
        run_id,
        gateway,
        embedder,
        pii_detector,
        geocoder,
        owner_user_id=owner_user_id,
        master_key=master_key,
        allow_special_category=True,
        n_runs=n_runs,
        temperature=temperature,
        judge=judge,
    )
    return run_id
