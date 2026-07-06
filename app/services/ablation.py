"""Attribution by ablation (defend/attribution-ablation.md, M3.2) — which items *cause* the leak.

The causal counterpart to the attack-side proxy: measure which posts actually drive the inference
(not just which the model cited), fill `marginal_effect`, and produce the actionable **"edit these
N posts"** minimal set. Everything is measured against the held-out independent adversary (M3.1).

Naive leave-one-out fails on redundancy — if two posts *each* pin your city, removing either alone
changes nothing, so both score ~0 ("nothing to edit") and the "six boring posts together" case is
missed. The fix is a **greedy minimal-sufficient-subset search**: greedily remove the item whose
removal most weakens the adversary (cheap N=1 probes), until the true value is no longer recoverable
(out of top-3), then **prune** items that turned out unnecessary. Greedy removes redundant pins in
sequence, so both end up flagged. Cost is linear in k × cheap probes, bounded by a hard cap.

`marginal_effect` is the raw adversary-confidence drop attributed at an item's removal step. The
calibrated, noise-floor-gated version + the full-N paired-bootstrap confirmation land at M3.4.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.domain.output_schema import AttributeCode
from app.services.adversary import Adversary, AdversaryFinding, adversary_attack
from app.services.geocoding import Geocoder
from app.services.occupation import OccupationJudge

logger = logging.getLogger(__name__)

_DEFAULT_MAX_PROBES = 60  # hard cost ceiling on adversary passes (attribution-ablation §8)

# One content item under ablation: (item id, text). The id keys `marginal_effect`.
Item = tuple[str, str]


@dataclass(frozen=True)
class AblationResult:
    """The causal attribution: the minimal edit set + each item's marginal effect on the adversary.

    `minimal_set` is the actionable "edit these N" target (empty if the adversary never recovered,
    or if no subset within the probed items broke recovery). `sufficient` is whether recovery was
    actually broken. `marginal_effect` is non-zero only for the minimal-set members.
    """

    minimal_set: list[str]
    marginal_effect: dict[str, float]
    sufficient: bool
    probes: int


@dataclass(frozen=True)
class _Probe:
    """One adversary result for a content subset: the finding + its top-1 confidence."""

    finding: AdversaryFinding
    confidence: float


# Runs one N=1 adversary probe over the given present item ids.
_ProbeFn = Callable[[set[str]], Awaitable["_Probe"]]


def _top_confidence(finding: AdversaryFinding) -> float:
    """The adversary's confidence in its top-1 guess (0.0 when it abstained)."""
    if finding.guess.status != "inferred" or not finding.guess.candidates:
        return 0.0
    return finding.guess.candidates[0].confidence.raw


async def find_minimal_set(
    adversary: Adversary,
    content: Sequence[Item],
    *,
    target_attribute: AttributeCode,
    true_value: object,
    geocoder: Geocoder,
    judge: OccupationJudge | None = None,
    max_probes: int = _DEFAULT_MAX_PROBES,
) -> AblationResult:
    """Greedily find the subset-minimal set of items to remove so the adversary can't recover.

    Runs cheap N=1 adversary probes on `content` (the original, to *find* the target; the before/
    after later *proves* it). Every attribute is a distinct call — scope to the affected attribute
    upstream. Content decrypted in memory, never logged.
    """
    items = {item_id: text for item_id, text in content}
    marginal_effect: dict[str, float] = dict.fromkeys(items, 0.0)
    probes = 0

    async def probe(present_ids: set[str]) -> _Probe:
        nonlocal probes
        probes += 1
        finding = await adversary_attack(
            adversary,
            [(item_id, items[item_id]) for item_id in items if item_id in present_ids],
            target_attribute=target_attribute,
            true_value=true_value,
            geocoder=geocoder,
            judge=judge,
            n_runs=1,
        )
        return _Probe(finding=finding, confidence=_top_confidence(finding))

    present = set(items)
    baseline = await probe(present)
    if not baseline.finding.value_recovered:
        # the adversary never recovered on the original — nothing to attribute (cross-model
        # divergence with the Profiler is surfaced, not hidden).
        return AblationResult(
            minimal_set=[], marginal_effect=marginal_effect, sufficient=True, probes=probes
        )

    removed: list[str] = []
    confidence = baseline.confidence
    recovered = True
    while recovered and present and probes < max_probes:
        best_id: str | None = None
        best: _Probe | None = None
        for item_id in sorted(present):  # deterministic order for stable ties
            if probes >= max_probes:
                break
            candidate = await probe(present - {item_id})
            if best is None or _better_removal(candidate, best):
                best_id, best = item_id, candidate
        if best_id is None or best is None:
            break
        marginal_effect[best_id] = max(0.0, confidence - best.confidence)
        removed.append(best_id)
        present.discard(best_id)
        confidence = best.confidence
        recovered = best.finding.value_recovered

    if recovered:
        # the search never broke recovery (the signal is spread beyond the ablated items, or the
        # probe cap was hit): the greedily-removed items are NOT an actionable set — editing them
        # wouldn't fix the leak — so report the honest "couldn't localize a cause", not a bogus
        # target (attribution-ablation.md §6; never a false sense of safety).
        if probes >= max_probes:
            logger.warning("ablation hit the probe cap (%d) before breaking recovery", max_probes)
        return AblationResult(
            minimal_set=[],
            marginal_effect=dict.fromkeys(items, 0.0),
            sufficient=False,
            probes=probes,
        )

    minimal_set = await _prune(
        probe, removed, set(items), marginal_effect, budget=lambda: probes < max_probes
    )
    return AblationResult(
        minimal_set=minimal_set, marginal_effect=marginal_effect, sufficient=True, probes=probes
    )


def _better_removal(candidate: _Probe, best: _Probe) -> bool:
    """Prefer a removal that breaks recovery; otherwise the one that drops confidence most."""
    if candidate.finding.value_recovered != best.finding.value_recovered:
        return not candidate.finding.value_recovered  # a flip to not-recovered wins
    return candidate.confidence < best.confidence


async def _prune(
    probe: _ProbeFn,
    removed: list[str],
    all_ids: set[str],
    marginal_effect: dict[str, float],
    *,
    budget: Callable[[], bool],
) -> list[str]:
    """Drop any removed item whose removal wasn't needed — recovery stays broken with it added back.

    Subset-minimality: an item is kept only if re-adding it (while everything else stays removed)
    lets the adversary recover again. A pruned item's marginal effect is reset to ~0. Stops at the
    probe budget, keeping the not-yet-pruned items (conservative — flags more, never fewer).
    """
    minimal = list(removed)
    for item_id in removed:
        if not budget():  # hit the hard cost ceiling → keep the rest of the set
            break
        present = all_ids - (set(minimal) - {item_id})  # re-add just this one
        result = await probe(present)
        if not result.finding.value_recovered:  # still broken without removing it → not needed
            minimal.remove(item_id)
            marginal_effect[item_id] = 0.0
    return minimal
