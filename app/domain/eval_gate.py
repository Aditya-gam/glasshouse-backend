"""Eval floor gate (10-testing/eval-as-ci-gate.md, M2.6) — pure, no IO.

The regression guard: a benchmark whose per-attribute top-1 accuracy drops below the committed
floor (set from the first clean run) **fails the build**. Catches a prompt / model / pipeline /
retrieval / self-consistency / normalizer / match-rule change that silently hurts accuracy — the
hard-invalidation coupling. Pure, so the CI gate (deterministic fixture) and the operator script
(a real benchmark's `eval_results`) share one rule.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FloorBreach:
    """One attribute whose measured top-1 accuracy fell below its floor (or is missing entirely)."""

    attribute: str
    top1_acc: float
    floor: float


def check_eval_floor(
    top1_by_attribute: Mapping[str, float], floor: Mapping[str, float]
) -> list[FloorBreach]:
    """The floor breaches, if any — an empty list means the build passes.

    Every attribute named in the floor must meet its minimum; an attribute that silently dropped
    out of the benchmark counts as 0.0 (a breach), so a regression that stops inferring an
    attribute is caught, not hidden.
    """
    breaches: list[FloorBreach] = []
    for attribute, minimum in floor.items():
        top1_acc = top1_by_attribute.get(attribute, 0.0)
        if top1_acc < minimum:
            breaches.append(FloorBreach(attribute=attribute, top1_acc=top1_acc, floor=minimum))
    return breaches
