"""Eval service (M2.2) — benchmark the attack engine on the seeded SynthPAI profiles (Job 1).

Runs the **same** engine (`infer_profile`) over each benchmark persona on a **privileged**
connection (like the seed — `eval_labels` has no app-role grant, so a user request can never reach
ground truth), persists each persona's consensus guesses as `inferences` under one `eval` run, and
scores every prediction against the persona's labels with the shared match rules
(`domain.eval_match`). Aggregates to per-attribute `eval_results` (top-1/top-3, by hardness). Only
labels a comment actually reveals (certainty ≥ 1) count toward accuracy (benchmarking.md). No user
data; content decrypted in memory, never logged.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.attributes import BY_CODE
from app.domain.eval_match import (
    LabeledPrediction,
    MatchVerdict,
    ScoredAttribute,
    match_prediction,
    score_predictions,
)
from app.domain.output_schema import AttributeGuess
from app.gateway.client import Profiler
from app.gateway.prompts import ENGINE_VERSION
from app.repositories import eval_labels as eval_labels_repo
from app.repositories import eval_results as eval_results_repo
from app.repositories import profiles as profiles_repo
from app.repositories import run_metrics as run_metrics_repo
from app.repositories import runs as runs_repo
from app.retrieval.embedder import Embedder
from app.retrieval.pii import PiiDetector
from app.services.benchmark import SYNTHPAI_USER_ID
from app.services.geocoding import Geocoder
from app.services.inference import infer_profile, persist_attribute_guess
from app.services.occupation import OccupationJudge

_MODALITY = "text"
_MISS = MatchVerdict(top1=False, top3=False)  # a revealed label the engine didn't infer at all


@dataclass(frozen=True)
class EvalRunResult:
    """One benchmark pass: the eval run id, personas scored, and per-attribute accuracy."""

    run_id: uuid.UUID
    personas: int
    scores: list[ScoredAttribute]


def _revealed(true_value: dict[str, Any]) -> tuple[object, int | None] | None:
    """The (label value, hardness) to score, or None when no comment reveals it (certainty 0)."""
    if int(true_value.get("certainty", 0)) < 1:
        return None
    hardness = true_value.get("hardness")
    return true_value.get("value"), (int(hardness) if hardness is not None else None)


def _score_persona(
    guesses: list[AttributeGuess], labels: list[eval_labels_repo.EvalLabelRow]
) -> list[LabeledPrediction]:
    """Match one persona's predictions against its revealed labels (a miss if not predicted)."""
    by_attribute = {guess.attribute: guess for guess in guesses}
    scored: list[LabeledPrediction] = []
    for label in labels:
        attribute = label.attribute_code
        if attribute not in BY_CODE:
            continue  # defensive: only the 8 known attributes are scorable
        revealed = _revealed(label.true_value)
        if revealed is None:
            continue
        value, hardness = revealed
        guess = by_attribute.get(attribute)
        verdict = match_prediction(attribute, guess, value) if guess is not None else _MISS
        scored.append(LabeledPrediction(attribute=attribute, verdict=verdict, hardness=hardness))
    return scored


async def run_eval(
    conn: AsyncConnection,
    gateway: Profiler,
    embedder: Embedder,
    pii_detector: PiiDetector,
    geocoder: Geocoder,
    *,
    master_key: str,
    judge: OccupationJudge | None = None,
    limit: int | None = None,
    n_runs: int | None = None,
    temperature: float | None = None,
) -> EvalRunResult:
    """Benchmark the engine over the (optionally sliced) SynthPAI profiles → one eval run + results.

    The eval run is anchored to a benchmark-session profile (owns no items); each persona's
    predictions persist as `inferences` under it (tagged with the persona's own profile_id), and
    `eval_results` hangs off the run. Privileged connection.
    """
    session_profile_id = await profiles_repo.get_or_create_self_profile(conn, SYNTHPAI_USER_ID)
    run_id = await runs_repo.insert_run_v2(
        conn, session_profile_id, run_type="eval", status="running", engine_version=ENGINE_VERSION
    )
    persona_ids = await profiles_repo.list_profile_ids(
        conn, user_id=SYNTHPAI_USER_ID, profile_type="synthpai", limit=limit
    )
    started = time.monotonic()
    scored: list[LabeledPrediction] = []
    model_calls = 0
    for persona_id in persona_ids:
        inference = await infer_profile(
            conn,
            persona_id,
            gateway,
            embedder,
            pii_detector,
            geocoder,
            master_key=master_key,
            allow_special_category=True,  # synthetic data — benchmark all 8 incl. Art. 9 birthplace
            n_runs=n_runs,
            temperature=temperature,
            judge=judge,
        )
        model_calls += inference.model_calls
        for guess in inference.guesses:
            await persist_attribute_guess(
                conn,
                guess,
                valid_item_ids=inference.valid_item_ids,
                owner_user_id=SYNTHPAI_USER_ID,
                profile_id=persona_id,
                run_id=run_id,
                master_key=master_key,
            )
        labels = await eval_labels_repo.list_labels_for_profile(conn, persona_id)
        scored.extend(_score_persona(inference.guesses, labels))

    scores = score_predictions(scored)
    for score in scores:
        await eval_results_repo.insert_eval_result(
            conn,
            run_id=run_id,
            attribute_code=score.attribute,
            modality=_MODALITY,
            top1_acc=score.top1_acc,
            top3_acc=score.top3_acc,
            by_hardness=score.by_hardness,
            engine_version=ENGINE_VERSION,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    await run_metrics_repo.insert_run_metrics(
        conn, run_id=run_id, latency_ms=latency_ms, model_calls=model_calls
    )
    await runs_repo.set_run_status_where(
        conn, run_id, "succeeded", allowed_from=("running",), finished=True
    )
    return EvalRunResult(run_id=run_id, personas=len(persona_ids), scores=scores)
