"""Anonymizer loop (defend/text-remediation.md, M3.3) — the advise-only truthful editor.

Produces the localized, generalization-first rewrite that **breaks the inference while preserving
meaning**, for one item in the ablation minimal set (affected attribute only). Iterative
refine-against-feedback (FgAA/RUPTA), capped at **k≈3 hops**: the anonymizer edits → the held-out
**feedback** adversary re-attacks the edit → still recovers? the anonymizer refines using that
feedback and loops → broken or k reached → stop.

The loop is refined against the *feedback* adversary but the final before/after is **proven** by a
different, held-out *evaluator* adversary (M3.1, at M3.7) — anonymizer ≠ feedback-adversary ≠
evaluator-adversary ≠ judges ≠ profiler (the separation chain), so the editor can't game the proof.
**Truthful edits only** (decoy is the opt-in M3.6). Advise-only: this returns a *suggested* edit,
never writes anywhere. Content decrypted in memory, never logged.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from app.domain.eval_match import render_value
from app.domain.output_schema import AttributeCode
from app.gateway.client import AnonymizerEdit, EditOperation
from app.services.adversary import AdversaryFinding, attack_content
from app.services.geocoding import Geocoder
from app.services.inference import ProfileFn
from app.services.occupation import OccupationJudge

logger = logging.getLogger(__name__)

_DEFAULT_HOPS = 3  # k≈3 — the FgAA/RUPTA feedback budget


class Anonymizer(Protocol):
    """One truthful localized edit (the gateway `anonymizer` slot; test fakes conform)."""

    async def anonymize(
        self, *, text: str, spans: list[str], attribute: str, feedback: str | None = None
    ) -> AnonymizerEdit: ...


@dataclass(frozen=True)
class AnonymizeResult:
    """The suggested edit for one item + whether it broke the feedback adversary's recovery."""

    item_id: str
    original_text: str
    edited_text: str
    operations: list[EditOperation]
    hops: int
    broke: bool  # the feedback adversary no longer recovers the true value from the edit


def _feedback_text(finding: AdversaryFinding) -> str:
    """What the adversary still latched onto, for the anonymizer to refine against next hop."""
    if not finding.guess.candidates:
        return "the attribute (no specific cue given)"
    value = render_value(finding.attribute, finding.guess.candidates[0].value)
    reasoning = (finding.guess.reasoning or "").strip()
    return f"{value} (cue: {reasoning})" if reasoning else value


async def anonymize_item(
    anonymizer: Anonymizer,
    feedback_profile_fn: ProfileFn,
    item: tuple[str, str],
    *,
    target_attribute: AttributeCode,
    true_value: object,
    spans: list[str],
    geocoder: Geocoder,
    judge: OccupationJudge | None = None,
    hops: int = _DEFAULT_HOPS,
) -> AnonymizeResult:
    """Refine an edit for `item` until the held-out feedback adversary can't recover `true_value`.

    Each hop: the anonymizer produces a truthful localized edit (given the prior feedback), then the
    feedback adversary (a distinct slot) re-attacks the edit blind. Stops when it can no longer
    recover, or at the k-hop cap (reported honestly as not-broken — no false success).
    """
    item_id, original = item
    edited = original
    feedback: str | None = None
    operations: list[EditOperation] = []
    for hop in range(1, hops + 1):
        edit = await anonymizer.anonymize(
            text=edited, spans=spans, attribute=target_attribute, feedback=feedback
        )
        edited = edit.edited_text
        operations.append(edit.operation)
        finding = await attack_content(
            feedback_profile_fn,
            [(item_id, edited)],
            target_attribute=target_attribute,
            true_value=true_value,
            geocoder=geocoder,
            judge=judge,
            n_runs=1,
        )
        if not finding.value_recovered:
            return AnonymizeResult(item_id, original, edited, operations, hop, broke=True)
        feedback = _feedback_text(finding)
    logger.info("anonymizer hit the %d-hop cap without breaking %s", hops, target_attribute)
    return AnonymizeResult(item_id, original, edited, operations, hops, broke=False)
