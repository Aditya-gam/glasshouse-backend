"""Pure assembly of the defend-screen `RemediationRead` from persisted frontier rows (M3.8).

No IO: the router fetches the rows + target + original item texts, then this shapes them into the
DTO. `status` is honest — `proven` only when an option both clears the noise floor and flips the
value-recovery, `within_noise` when options exist but none is proven, `cant_break` when the leak
could not be localized (no options). The FE receives each option's original + edited text and
computes the highlight diff itself (segs is left unset). Content is never logged.
"""

from decimal import Decimal
from typing import Any, Literal, cast

from app.api.v1.schemas import (
    DefendEdit,
    DefendOptionRead,
    DefendTarget,
    Reliability,
    RemediationRead,
)
from app.repositories.remediations import FrontierRow

_OptionKey = Literal["minimal", "stronger", "remove", "decoy"]

_OPTION_NAME: dict[str, str] = {
    "minimal": "Light edit",
    "stronger": "Stronger edit",
    "remove": "Remove",
    "decoy": "Decoy (opt-in)",
}
_OPTION_DESC: dict[str, str] = {
    "minimal": "Generalize the leaking cue — keeps the most of what you said.",
    "stronger": "A broader abstraction — more privacy, less detail.",
    "remove": "Delete the item — maximum truthful privacy.",
    "decoy": "Publish a plausible FALSE cue — deception; the truthful options are shown alongside.",
}
_UTILITY_LABEL: dict[str, str] = {
    "fully": "Fully preserved",
    "mostly": "Mostly preserved",
    "partially": "Partially preserved",
    "lost": "Meaning lost",
}


def _reliability(interval: dict[str, Any], point: Decimal) -> Reliability:
    """A Reliability from a persisted {point, lo, hi} interval (degenerate point on a miss)."""
    p = float(point)
    return Reliability(
        point=float(interval.get("point", p)),
        lo=float(interval.get("lo", p)),
        hi=float(interval.get("hi", p)),
    )


def _utility_int(utility_score: dict[str, Any]) -> int | None:
    """The 0..100 utility percentage from the stored 0..1 score, or None when absent."""
    raw = utility_score.get("utility_score")
    return None if raw is None else round(float(raw) * 100)


def _flips(row: FrontierRow) -> bool:
    """Whether this option both clears the noise floor and flips value-recovery (the proven win)."""
    return row.significant and row.value_recovery_before and not row.value_recovery_after


def _edits(row: FrontierRow, item_texts: dict[str, str]) -> list[DefendEdit]:
    """One DefendEdit per change; the FE diffs `original` vs `edited` (segs left unset)."""
    edits: list[DefendEdit] = []
    for change in row.span_changes:
        item_id = str(change.get("item_id", ""))
        removed = change.get("op") == "remove_item"
        edits.append(
            DefendEdit(
                src=item_id,
                date="",  # the item's post date is not surfaced in the read yet (v1)
                original=item_texts.get(item_id),
                edited=None if removed else change.get("replacement"),
                remove=True if removed else None,
                decoy=True if row.is_decoy else None,
                note=row.misled_value if row.is_decoy else None,
            )
        )
    return edits


def _option(row: FrontierRow, item_texts: dict[str, str]) -> DefendOptionRead:
    return DefendOptionRead(
        key=cast(_OptionKey, row.option_key),
        name=_OPTION_NAME.get(row.option_key, row.option_key),
        desc=_OPTION_DESC.get(row.option_key, ""),
        truthful=not row.is_decoy,
        opt_in=True if row.is_decoy else None,
        remove=True if row.action == "remove" else None,
        after=_reliability(row.ci_after, row.confidence_after),
        recovered=row.value_recovery_after,
        misled=row.misled_value,
        utility=_utility_int(row.utility_score),
        utility_label=_UTILITY_LABEL.get(str(row.utility_score.get("meaning")), "Unknown"),
        edits=_edits(row, item_texts),
    )


def _status(rows: list[FrontierRow]) -> Literal["proven", "within_noise", "cant_break"]:
    """proven if any option is a proven win; within_noise if options exist but none is; else cant_break."""  # noqa: E501
    if not rows:
        return "cant_break"
    return "proven" if any(_flips(row) for row in rows) else "within_noise"


def assemble_remediation(
    *,
    attribute: str,
    value: str,
    before: Reliability,
    rows: list[FrontierRow],
    item_texts: dict[str, str],
) -> RemediationRead:
    """Shape the persisted frontier into the advise-only `RemediationRead` (target + options)."""
    return RemediationRead(
        status=_status(rows),
        target=DefendTarget(attribute=attribute, value=value, before=before),
        options=[_option(row, item_texts) for row in rows],
    )
