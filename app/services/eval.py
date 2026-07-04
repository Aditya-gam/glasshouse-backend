"""Eval service (M2.2/M2.3) — benchmark the attack engine on the seeded SynthPAI profiles (Job 1).

Runs the **same** engine (`infer_profile`) over each benchmark persona on a **privileged**
connection (like the seed — `eval_labels` has no app-role grant, so a user request can never reach
ground truth), persists each persona's consensus guesses as `inferences` under one `eval` run, and
scores every prediction against the persona's labels with the shared match rules
(`domain.eval_match`). Ambiguous occupation/geo cases escalate to the M2.3 match-judge; a `partial`
or low-confidence verdict raises a human spot-check. Aggregates to per-attribute `eval_results`
(top-1/top-3, by hardness). Only labels a comment actually reveals (certainty ≥ 1) count toward
accuracy (benchmarking.md). No user data; content decrypted in memory, never logged.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.attributes import BY_CODE
from app.domain.calibration import CalibrationSample, build_calibration
from app.domain.eval_match import (
    LabeledPrediction,
    MatchVerdict,
    ScoredAttribute,
    score_predictions,
)
from app.domain.output_schema import AttributeGuess
from app.gateway.client import Profiler
from app.gateway.prompts import ENGINE_VERSION, MATCH_JUDGE_VERSION
from app.repositories import calibration as calibration_repo
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
from app.services.match_judge import MatchJudge, SpotCheck, judge_match_prediction
from app.services.occupation import OccupationJudge

logger = logging.getLogger(__name__)

_MODALITY = "text"


@dataclass(frozen=True)
class EvalRunResult:
    """One benchmark pass: the eval run id, personas scored, per-attribute accuracy, spot-checks."""

    run_id: uuid.UUID
    personas: int
    scores: list[ScoredAttribute]
    spot_checks: list[SpotCheck]


def _revealed(true_value: dict[str, Any]) -> tuple[object, int | None] | None:
    """The (label value, hardness) to score, or None when no comment reveals it (certainty 0)."""
    if int(true_value.get("certainty", 0)) < 1:
        return None
    hardness = true_value.get("hardness")
    return true_value.get("value"), (int(hardness) if hardness is not None else None)


@dataclass(frozen=True)
class _PersonaScore:
    """One persona's scoring: verdicts (for accuracy), spot-checks, calibration samples."""

    predictions: list[LabeledPrediction]
    spot_checks: list[SpotCheck]
    samples: list[CalibrationSample]


def _calibration_sample(guess: AttributeGuess, correct: bool) -> CalibrationSample | None:
    """A calibration sample from an inferred prediction's top-1 candidate (None if abstained).

    The raw signal + N come off the candidate's confidence, so the map keys exactly to what M2.5
    will look up. Abstained guesses (no candidate) are not calibrated — nothing is shown for them.
    """
    if not guess.candidates:
        return None
    confidence = guess.candidates[0].confidence
    n = confidence.agreement.n_runs if confidence.agreement is not None else 1
    return CalibrationSample(
        attribute=guess.attribute,
        signal=confidence.source,
        n=n,
        raw_confidence=confidence.raw,
        correct=correct,
    )


async def _score_persona(
    guesses: list[AttributeGuess],
    labels: list[eval_labels_repo.EvalLabelRow],
    *,
    match_judge: MatchJudge | None,
) -> _PersonaScore:
    """Match one persona's predictions against its revealed labels (a miss if not predicted).

    Ambiguous occupation/geo misses escalate to the match-judge; the top-1 verdict may raise a
    spot-check. A revealed label the engine never guessed is a miss (never sent to the judge). Each
    inferred top-1 prediction also yields a calibration sample (raw confidence + correctness).
    """
    by_attribute = {guess.attribute: guess for guess in guesses}
    predictions: list[LabeledPrediction] = []
    spot_checks: list[SpotCheck] = []
    samples: list[CalibrationSample] = []
    for label in labels:
        attribute = label.attribute_code
        if attribute not in BY_CODE:
            continue  # defensive: only the 8 known attributes are scorable
        revealed = _revealed(label.true_value)
        if revealed is None:
            continue
        value, hardness = revealed
        guess = by_attribute.get(attribute)
        if guess is None:
            verdict = MatchVerdict(top1=False, top3=False)  # revealed but never inferred → miss
        else:
            judged = await judge_match_prediction(attribute, guess, value, judge=match_judge)
            verdict = judged.verdict
            if judged.spot_check is not None:
                spot_checks.append(judged.spot_check)
            sample = _calibration_sample(guess, verdict.top1)
            if sample is not None:
                samples.append(sample)
        predictions.append(
            LabeledPrediction(attribute=attribute, verdict=verdict, hardness=hardness)
        )
    return _PersonaScore(predictions=predictions, spot_checks=spot_checks, samples=samples)


async def run_eval(
    conn: AsyncConnection,
    gateway: Profiler,
    embedder: Embedder,
    pii_detector: PiiDetector,
    geocoder: Geocoder,
    *,
    master_key: str,
    judge: OccupationJudge | None = None,
    match_judge: MatchJudge | None = None,
    limit: int | None = None,
    n_runs: int | None = None,
    temperature: float | None = None,
) -> EvalRunResult:
    """Benchmark the engine over the (optionally sliced) SynthPAI profiles → one eval run + results.

    The eval run is anchored to a benchmark-session profile (owns no items); each persona's
    predictions persist as `inferences` under it (tagged with the persona's own profile_id), and
    `eval_results` hangs off the run. When a `match_judge` is given, ambiguous occupation/geo cases
    are LLM-judged and the scoring version pins the judge (judge↔calibration coupling). Privileged
    connection.
    """
    scoring_version = (
        ENGINE_VERSION if match_judge is None else f"{ENGINE_VERSION}+{MATCH_JUDGE_VERSION}"
    )
    session_profile_id = await profiles_repo.get_or_create_self_profile(conn, SYNTHPAI_USER_ID)
    run_id = await runs_repo.insert_run_v2(
        conn, session_profile_id, run_type="eval", status="running", engine_version=ENGINE_VERSION
    )
    persona_ids = await profiles_repo.list_profile_ids(
        conn, user_id=SYNTHPAI_USER_ID, profile_type="synthpai", limit=limit
    )
    started = time.monotonic()
    scored: list[LabeledPrediction] = []
    spot_checks: list[SpotCheck] = []
    samples: list[CalibrationSample] = []
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
        persona = await _score_persona(inference.guesses, labels, match_judge=match_judge)
        scored.extend(persona.predictions)
        spot_checks.extend(persona.spot_checks)
        samples.extend(persona.samples)

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
            engine_version=scoring_version,
        )
    # The map is keyed by the ATTACK engine_version (bare), not the scoring version, because a
    # user's inference pins the bare engine and looks the map up by it (calibration.md: the
    # engine_version "must match the inference's"). The judge that measured the accuracy is pinned
    # on eval_results, not on the map — a judge change → re-run the benchmark → the map upserts.
    for curve in build_calibration(samples):
        for bucket in curve.buckets:
            await calibration_repo.upsert_calibration_bucket(
                conn,
                engine_version=ENGINE_VERSION,
                attribute_code=curve.attribute,
                modality=_MODALITY,
                signal=curve.signal,
                n=curve.n,
                confidence_bucket=bucket.bucket,
                empirical_accuracy=bucket.empirical_accuracy,
                ece=curve.ece,
            )
    latency_ms = int((time.monotonic() - started) * 1000)
    await run_metrics_repo.insert_run_metrics(
        conn, run_id=run_id, latency_ms=latency_ms, model_calls=model_calls
    )
    await runs_repo.set_run_status_where(
        conn, run_id, "succeeded", allowed_from=("running",), finished=True
    )
    logger.info(
        "eval run %s: %d personas, %d spot-checks flagged",
        run_id,
        len(persona_ids),
        len(spot_checks),
    )
    return EvalRunResult(
        run_id=run_id, personas=len(persona_ids), scores=scores, spot_checks=spot_checks
    )
