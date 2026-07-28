"""Remediation worker core (defend/text-remediation.md, M3.7) — proven, advise-only before→after.

Ties the Defend engine into one run: **ablation** locates the load-bearing items (M3.2) → the
**anonymizer** rewrites each, truthfully, refining against the feedback adversary (M3.3) → the
**held-out evaluator adversary** re-attacks both the original and the edited content (M3.1), N times
each → the **noise floor + paired bootstrap** decides whether the drop is real (M3.4) → the
**utility judge** scores what the edit preserved (M3.5). The result is persisted to `remediations`
as a proven, advise-only suggestion — never applied, never implying false safety.

`plan_remediation` is the pure-ish core (all model access is injected, no DB) so it is unit-tested
with fakes; `execute_remediation_run` is the thin IO wrapper the worker calls. Content is decrypted
in memory only and never logged.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.attributes import BY_CODE
from app.domain.eval_match import render_value
from app.domain.normalize import income_bracket
from app.domain.output_schema import (
    AttributeCode,
    AttributeValue,
    NumericValue,
    RawAttributeGuess,
)
from app.domain.remediation import classify_action, majority_recovered, span_change
from app.domain.significance import Interval, RemediationDelta, assess_remediation
from app.gateway.prompts import ADVERSARY_VERSION
from app.repositories import calibration as calibration_repo
from app.repositories import inferences as inferences_repo
from app.repositories import remediations as remediations_repo
from app.repositories import run_metrics as run_metrics_repo
from app.repositories import runs as runs_repo
from app.retrieval.embedder import Embedder
from app.retrieval.pii import PiiDetector
from app.retrieval.retriever import retrieve_evidence
from app.services.ablation import find_minimal_set
from app.services.adversary import Adversary, AdversaryFinding, adversary_attack
from app.services.anonymize import Anonymizer, AnonymizeResult, anonymize_item
from app.services.consent import ConsentRequiredError
from app.services.geocoding import Geocoder
from app.services.inference import ProfileFn
from app.services.occupation import OccupationJudge
from app.services.utility import UtilityAssessment, UtilityJudge, assess_utility

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLES = 5  # N paired re-attacks per endpoint for the bootstrap (noise-floor doc §N≈5)
# Provisional noise-floor margin used until the Job-1 noise model is estimated (noise_std is NULL):
# a conservative confidence-drop the paired bootstrap CI must clear to call a drop significant.
_PROVISIONAL_FLOOR_MARGIN = 0.10
_BOOTSTRAP_SEED = 0  # deterministic significance (reproducible per noise-floor-and-variance.md)

_ATTRIBUTE_VALUE_ADAPTER: TypeAdapter[AttributeValue] = TypeAdapter(AttributeValue)


class RemediationGateway(Adversary, Anonymizer, UtilityJudge, Protocol):
    """Every model slot the worker needs (GatewayClient conforms; test fakes conform).

    The evaluator adversary (`adversary_profile_all`), the anonymizer (`anonymize`), and the utility
    judge (`judge_utility`) come from the composed protocols; the anonymizer loop's held-out
    feedback adversary is the one extra method below (a distinct slot from the evaluator).
    """

    async def feedback_profile_all(
        self, *, content: str, temperature: float = 0.0
    ) -> list[RawAttributeGuess]: ...


@dataclass(frozen=True)
class RemediationOutcome:
    """The proven, advise-only remediation to persist (one row); None when nothing to suggest."""

    action: str  # rewrite | remove
    primary_edited_text: str | None  # the load-bearing item's rewrite (None when it was removed)
    span_changes: list[dict[str, Any]]  # per-item localized edits (JSONB)
    delta: RemediationDelta  # before/after intervals + significant + value-recovery flip
    recovered_before: bool
    recovered_after: bool
    utility: UtilityAssessment
    evaluator_engine_version: str


def _top_confidence(finding: AdversaryFinding) -> float:
    """The adversary's top-1 confidence for the target attribute (0.0 when it abstained)."""
    if finding.guess.status != "inferred" or not finding.guess.candidates:
        return 0.0
    return finding.guess.candidates[0].confidence.raw


def _defended_value(attribute: AttributeCode, value: AttributeValue) -> str:
    """The value the recovery check compares against (income → its bracket word, not the estimate).

    The income match rule (eval_match) compares the adversary's bracket to a *label word*
    (low/medium/high); rendering the raw estimate ("95000") would never match, so income would look
    permanently un-recoverable. Every other attribute renders to a label-compatible string.
    """
    if attribute == "income" and isinstance(value, NumericValue):
        return value.bracket or income_bracket(value.estimate)
    return render_value(attribute, value)


async def _sample_endpoint(
    adversary: Adversary,
    content: Sequence[tuple[str, str]],
    *,
    target_attribute: AttributeCode,
    true_value: object,
    geocoder: Geocoder,
    judge: OccupationJudge | None,
    n_samples: int,
) -> tuple[list[float], list[bool]]:
    """Re-attack `content` `n_samples`× (each a stochastic ensemble) → paired confidences, recovery.

    Each sample is a full ensemble attack (temperature > 0, so the runs genuinely differ), repeated
    to measure the adversary's run-to-run spread for the paired bootstrap. Blind: only content
    reaches the model.
    """
    confidences: list[float] = []
    recoveries: list[bool] = []
    for _ in range(n_samples):
        finding = await adversary_attack(
            adversary,
            content,
            target_attribute=target_attribute,
            true_value=true_value,
            geocoder=geocoder,
            judge=judge,
        )
        confidences.append(_top_confidence(finding))
        recoveries.append(finding.value_recovered)
    return confidences, recoveries


async def plan_remediation(
    *,
    content: Sequence[tuple[str, str]],
    target_attribute: AttributeCode,
    true_value: str,
    adversary: Adversary,
    feedback_profile_fn: ProfileFn,
    anonymizer: Anonymizer,
    utility_judge: UtilityJudge,
    embedder: Embedder,
    geocoder: Geocoder,
    judge: OccupationJudge | None,
    floor_margin: float,
    n_samples: int = _DEFAULT_SAMPLES,
    seed: int = _BOOTSTRAP_SEED,
) -> RemediationOutcome | None:
    """Prove one remediation for `target_attribute` over `content`; None when nothing can be shown.

    Ablation finds the load-bearing set; if none is sufficient the leak isn't localizable — we
    return None (the read layer reports this honestly as `cant_break`, no false success). Otherwise
    every set item is truthfully rewritten, the edit is proven by re-attacking the original vs the
    edited content N times each, gated by the noise floor. A row is returned even when the edit does
    NOT break recovery (significant False / recovery still True) so the honest non-success is kept.
    """
    ablation = await find_minimal_set(
        adversary,
        content,
        target_attribute=target_attribute,
        true_value=true_value,
        geocoder=geocoder,
        judge=judge,
    )
    if not ablation.sufficient or not ablation.minimal_set:
        logger.info("remediation: %s not localizable — no sufficient minimal set", target_attribute)
        return None

    text_by_id = dict(content)
    edits: list[AnonymizeResult] = []
    for item_id in ablation.minimal_set:
        edits.append(
            await anonymize_item(
                anonymizer,
                feedback_profile_fn,
                (item_id, text_by_id[item_id]),
                target_attribute=target_attribute,
                true_value=true_value,
                spans=[],  # v1: the anonymizer localizes from the attribute; evidence spans later
                geocoder=geocoder,
                judge=judge,
            )
        )
    edited_by_id = {edit.item_id: edit for edit in edits}
    edited_content = [
        (item_id, edited_by_id[item_id].edited_text if item_id in edited_by_id else item_text)
        for item_id, item_text in content
    ]

    before_conf, before_rec = await _sample_endpoint(
        adversary,
        content,
        target_attribute=target_attribute,
        true_value=true_value,
        geocoder=geocoder,
        judge=judge,
        n_samples=n_samples,
    )
    after_conf, after_rec = await _sample_endpoint(
        adversary,
        edited_content,
        target_attribute=target_attribute,
        true_value=true_value,
        geocoder=geocoder,
        judge=judge,
        n_samples=n_samples,
    )
    recovered_before = majority_recovered(before_rec)
    recovered_after = majority_recovered(after_rec)
    delta = assess_remediation(
        before_conf,
        after_conf,
        recovered_before=recovered_before,
        recovered_after=recovered_after,
        floor_margin=floor_margin,
        seed=seed,
    )

    # the primary item (largest marginal effect) carries the displayed rewrite + utility score.
    primary_id = max(ablation.minimal_set, key=lambda i: ablation.marginal_effect.get(i, 0.0))
    primary = edited_by_id[primary_id]
    primary_removed = primary.operations[-1] == "remove_item"
    utility = await assess_utility(
        utility_judge,
        embedder,
        original=text_by_id[primary_id],
        edited=primary.edited_text,
        sensitive_attribute=target_attribute,
    )
    span_changes = [
        asdict(span_change(edit.item_id, edit.operations[-1], edit.edited_text)) for edit in edits
    ]
    return RemediationOutcome(
        action=classify_action([edit.operations[-1] for edit in edits]),
        primary_edited_text=None if primary_removed else primary.edited_text,
        span_changes=span_changes,
        delta=delta,
        recovered_before=recovered_before,
        recovered_after=recovered_after,
        utility=utility,
        evaluator_engine_version=ADVERSARY_VERSION,
    )


async def resolve_floor_margin(conn: AsyncConnection, attribute_code: str) -> float:
    """The noise-floor margin for `attribute_code` — the calibrated `noise_std`, else provisional.

    Wires the Job-1 noise model for the **held-out adversary** engine (ADVERSARY_VERSION) — the one
    that actually re-attacks both endpoints, so the floor is *its* run-to-run variance, not the
    profiler's. Until it is estimated the lookup returns None and we fall back to a conservative
    provisional margin so significance never over-claims on an un-benchmarked engine.
    """
    noise_std = await calibration_repo.get_noise_std(
        conn, engine_version=ADVERSARY_VERSION, attribute_code=attribute_code
    )
    return float(noise_std) if noise_std is not None else _PROVISIONAL_FLOOR_MARGIN


def _utility_dict(utility: UtilityAssessment) -> dict[str, Any]:
    """The utility_score JSONB shape (meaning + readability + similarity + floor verdict)."""
    return {
        "utility_score": utility.utility_score,
        "meaning": utility.meaning,
        "readability": utility.readability,
        "embedding_similarity": utility.embedding_similarity,
        "passes_floor": utility.passes_floor,
    }


def _interval_dict(interval: Interval) -> dict[str, float]:
    """The {point, lo, hi} JSONB shape for a bootstrap interval."""
    return {"point": interval.point, "lo": interval.lo, "hi": interval.hi}


async def execute_remediation_run(
    conn: AsyncConnection,
    run_id: UUID,
    gateway: RemediationGateway,
    embedder: Embedder,
    pii_detector: PiiDetector,
    geocoder: Geocoder,
    *,
    owner_user_id: UUID,
    master_key: str,
    target_inference_id: UUID,
    allow_special_category: bool,
    judge: OccupationJudge | None = None,
) -> None:
    """Execute a pre-created remediation run: running → prove → persist `remediations` → succeeded.

    Fetches the targeted inference (its attribute + the top-1 value being defended), re-runs the
    same evidence retrieval the attack used, proves the edit, and persists one advise-only row.
    RLS-scoped; a target that is abstained, missing, or un-localizable succeeds with no row (honest,
    no false safety). Remediating an Art. 9 attribute (birthplace) re-processes special-category
    data, so it fails closed without `allow_special_category`. Content decrypted in memory, never
    logged.
    """
    if not await runs_repo.set_run_status_where(conn, run_id, "running", allowed_from=("queued",)):
        return  # not claimable (canceled before pickup, or already running)
    started = time.monotonic()
    target = await inferences_repo.get_inference_target(conn, target_inference_id, master_key)
    if target is None or target.value is None:
        # missing/RLS-hidden, or the inference abstained → nothing to remediate.
        await runs_repo.set_run_status_where(
            conn, run_id, "succeeded", allowed_from=("running",), finished=True
        )
        return
    # attribute_code is an FK to attributes.code, so it is always a valid AttributeCode.
    attribute = cast(AttributeCode, target.attribute_code)
    if BY_CODE[attribute].is_art9 and not allow_special_category:
        # re-attacking birthplace re-infers a special category — deny without Art. 9 consent.
        raise ConsentRequiredError("art9_inference")
    true_value = _defended_value(attribute, _ATTRIBUTE_VALUE_ADAPTER.validate_python(target.value))
    items = await retrieve_evidence(
        conn, embedder, pii_detector, profile_id=target.profile_id, master_key=master_key
    )
    content = [(str(item.id), item.text) for item in items]
    floor_margin = await resolve_floor_margin(conn, target.attribute_code)
    outcome = (
        await plan_remediation(
            content=content,
            target_attribute=attribute,
            true_value=true_value,
            adversary=gateway,
            feedback_profile_fn=lambda text, temperature: gateway.feedback_profile_all(
                content=text, temperature=temperature
            ),
            anonymizer=gateway,
            utility_judge=gateway,
            embedder=embedder,
            geocoder=geocoder,
            judge=judge,
            floor_margin=floor_margin,
        )
        if content
        else None
    )
    if outcome is not None:
        await remediations_repo.insert_remediation(
            conn,
            profile_id=target.profile_id,
            inference_id=target_inference_id,
            run_id=run_id,
            owner_user_id=owner_user_id,
            master_key=master_key,
            action=outcome.action,
            edited_text=outcome.primary_edited_text,
            span_changes=outcome.span_changes,
            confidence_before=outcome.delta.before.point,
            confidence_after=outcome.delta.after.point,
            ci_before=_interval_dict(outcome.delta.before),
            ci_after=_interval_dict(outcome.delta.after),
            significant=outcome.delta.significant,
            value_recovery_before=outcome.recovered_before,
            value_recovery_after=outcome.recovered_after,
            utility_score=_utility_dict(outcome.utility),
            is_decoy=False,
            evaluator_engine_version=outcome.evaluator_engine_version,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    # model_calls stays 0 for now — remediation spans several services (ablation, anonymize,
    # sampling), so per-stage call counting lands with gateway usage surfacing (like tokens/cost).
    await run_metrics_repo.insert_run_metrics(
        conn, run_id=run_id, latency_ms=latency_ms, model_calls=0
    )
    await runs_repo.set_run_status_where(
        conn, run_id, "succeeded", allowed_from=("running",), finished=True
    )
