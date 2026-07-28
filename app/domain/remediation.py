"""Pure helpers for the remediation worker (defend/text-remediation.md, M3.7) — no IO.

The remediation worker proves an edit by re-attacking N times with the held-out adversary. These
helpers collapse those N stochastic runs into the noise-robust booleans + the persisted shape the
row needs: the value-recovery verdict (a majority over the runs, not a single lucky/unlucky one) and
the action classification (a whole-item removal reads as `remove`, anything localized as `rewrite`).
Kept pure so they are deterministic and unit-tested in isolation (architecture: pure domain core).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.domain.output_schema import EditOperation

RemediationAction = Literal["rewrite", "remove"]


@dataclass(frozen=True)
class SpanChange:
    """One localized edit in a remediation's `span_changes` list (one per edited item)."""

    item_id: str
    op: EditOperation  # generalize | remove_span | remove_item
    replacement: str | None  # the edited text; None when the whole item is removed


def majority_recovered(recoveries: Sequence[bool]) -> bool:
    """Whether the true value was recovered in at least half of the N adversary runs.

    The value-recovery flip is the headline safety claim, so it must be robust to a single
    stochastic run; a majority (ties count as recovered — fail closed toward "still exposed") is the
    noise-robust reduction. Empty input → False (no evidence of recovery).
    """
    if not recoveries:
        return False
    return sum(recoveries) * 2 >= len(recoveries)


def classify_action(operations: Sequence[EditOperation]) -> RemediationAction:
    """`remove` iff every edit removed a whole item; otherwise `rewrite` (generalize/remove_span).

    The `remediations.action` column distinguishes a truthful rewrite from an outright removal so
    the frontier can label the option; a mix of ops is still a rewrite (the user keeps content).
    """
    if operations and all(op == "remove_item" for op in operations):
        return "remove"
    return "rewrite"


def span_change(item_id: str, op: EditOperation, edited_text: str) -> SpanChange:
    """Build one `SpanChange`; a whole-item removal carries no replacement text."""
    return SpanChange(
        item_id=item_id,
        op=op,
        replacement=None if op == "remove_item" else edited_text,
    )
