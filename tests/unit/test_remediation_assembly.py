"""Unit (M3.8): assemble_remediation — frontier rows → RemediationRead, honest status + options."""

from decimal import Decimal

from app.api.v1.remediation_assembly import assemble_remediation
from app.api.v1.schemas import Reliability
from app.repositories.remediations import FrontierRow

_BEFORE = Reliability(point=0.86, lo=0.80, hi=0.90)
_ITEMS = {"a": "I live near Seattle"}


def _row(
    option_key: str,
    *,
    significant: bool = True,
    flip: bool = True,
    is_decoy: bool = False,
    action: str = "rewrite",
    edited: str | None = "I live near a nearby city",
    misled: str | None = None,
) -> FrontierRow:
    removed = action == "remove"
    return FrontierRow(
        option_key=option_key,
        action=action,
        edited_text=edited,
        span_changes=[
            {
                "item_id": "a",
                "op": "remove_item" if removed else "generalize",
                "replacement": None if removed else edited,
            }
        ],
        misled_value=misled,
        confidence_before=Decimal("0.86"),
        confidence_after=Decimal("0.20"),
        ci_before={"point": 0.86, "lo": 0.80, "hi": 0.90},
        ci_after={"point": 0.20, "lo": 0.10, "hi": 0.30},
        significant=significant,
        value_recovery_before=True,
        value_recovery_after=not flip,
        utility_score={"utility_score": 0.75, "meaning": "mostly"},
        is_decoy=is_decoy,
    )


def test_proven_frontier_maps_options_and_edits() -> None:
    rows = [_row("minimal"), _row("stronger"), _row("remove", action="remove")]

    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=rows, item_texts=_ITEMS
    )

    assert rem.status == "proven"  # an option clears the floor AND flips recovery
    assert [o.key for o in rem.options] == ["minimal", "stronger", "remove"]
    assert rem.target.before == _BEFORE
    assert rem.target.value == "Seattle, WA"

    minimal = rem.options[0]
    assert minimal.truthful is True
    assert minimal.recovered is False
    assert minimal.after.point == 0.20
    assert minimal.after.lo == 0.10  # from ci_after
    assert minimal.utility == 75
    assert minimal.utility_label == "Mostly preserved"
    edit = minimal.edits[0]
    assert edit.original == "I live near Seattle"  # the FE diffs original vs edited
    assert edit.edited == "I live near a nearby city"
    assert edit.segs is None

    remove = rem.options[2]
    assert remove.remove is True
    assert remove.edits[0].edited is None
    assert remove.edits[0].remove is True


def test_within_noise_when_no_option_is_proven() -> None:
    rows = [_row("minimal", significant=False, flip=False)]

    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=rows, item_texts=_ITEMS
    )

    assert rem.status == "within_noise"  # options exist but none is a proven win


def test_significant_drop_without_a_recovery_flip_is_within_noise() -> None:
    # a confidence drop that clears the floor but the adversary STILL recovers the true value is not
    # a win — status must be within_noise, never a fake "proven" (the flip guard).
    rows = [_row("minimal", significant=True, flip=False)]

    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=rows, item_texts=_ITEMS
    )

    assert rem.status == "within_noise"


def test_recovery_flip_that_does_not_clear_the_floor_is_within_noise() -> None:
    # the true value flips out of the top-3, but the drop is within the noise floor — not proven
    # (the significance guard). Both guards must hold for `proven`.
    rows = [_row("minimal", significant=False, flip=True)]

    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=rows, item_texts=_ITEMS
    )

    assert rem.status == "within_noise"


def test_empty_frontier_is_cant_break() -> None:
    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=[], item_texts={}
    )

    assert rem.status == "cant_break"
    assert rem.options == []
    assert rem.target.before == _BEFORE  # the exposure is still shown


def test_decoy_option_is_flagged_and_carries_the_misled_value() -> None:
    rows = [_row("decoy", is_decoy=True, action="decoy", misled="Portland, OR")]

    rem = assemble_remediation(
        attribute="location", value="Seattle, WA", before=_BEFORE, rows=rows, item_texts=_ITEMS
    )

    decoy = rem.options[0]
    assert decoy.truthful is False
    assert decoy.opt_in is True
    assert decoy.misled == "Portland, OR"  # the wrong value the adversary now guesses
    assert decoy.edits[0].decoy is True
