"""Unit (M3.6): the decoy engine — false-cue injection, gated by the per-use confirm.

The pure engine refuses to fabricate without `confirmed` (belt-and-suspenders — no path may
auto-inject a falsehood); the standing-consent factor is proven separately in the consent-gate
integration test. Also asserts the decoy prompt carries the spans/attribute/target value and pins
the editor slot.
"""

import pytest

from app.gateway.client import DecoyEdit
from app.gateway.prompts import DECOY_VERSION, build_decoy_prompt
from app.services.consent import DecoyNotConfirmedError
from app.services.decoy import generate_decoy


class _FakeDecoyEditor:
    """Returns a fixed decoy edit; records calls so we can assert it is (not) spent."""

    def __init__(self, decoy_value: str = "nurse") -> None:
        self._decoy_value = decoy_value
        self.calls: list[dict[str, object]] = []

    async def decoy(
        self, *, text: str, spans: list[str], attribute: str, decoy_value: str | None = None
    ) -> DecoyEdit:
        self.calls.append(
            {"text": text, "spans": spans, "attribute": attribute, "decoy_value": decoy_value}
        )
        return DecoyEdit(
            reasoning="",
            edited_text="I just finished a long shift at the hospital.",
            decoy_value=decoy_value or self._decoy_value,
            note="implies a healthcare job",
        )


async def test_generate_decoy_injects_a_false_value_and_calls_editor_once() -> None:
    editor = _FakeDecoyEditor()

    result = await generate_decoy(
        editor,
        ("item-1", "I just pushed a big release at work."),
        target_attribute="occupation",
        spans=["pushed a big release"],
        confirmed=True,
        decoy_value="nurse",
    )

    assert result.item_id == "item-1"
    assert result.decoy_value == "nurse"  # the plausible-but-false value now implied
    assert result.edited_text != result.original_text
    assert len(editor.calls) == 1 and editor.calls[0]["decoy_value"] == "nurse"


async def test_generate_decoy_fails_closed_without_confirmation() -> None:
    editor = _FakeDecoyEditor()

    with pytest.raises(DecoyNotConfirmedError):
        await generate_decoy(
            editor,
            ("item-1", "I just pushed a big release at work."),
            target_attribute="occupation",
            spans=["pushed a big release"],
            confirmed=False,  # the per-use confirm is absent → never fabricate
        )

    assert editor.calls == []  # the engine refused before spending the model


async def test_generate_decoy_lets_the_model_pick_a_value_when_none_given() -> None:
    editor = _FakeDecoyEditor(decoy_value="teacher")

    result = await generate_decoy(
        editor,
        ("item-2", "Grading papers all weekend."),
        target_attribute="occupation",
        spans=["Grading papers"],
        confirmed=True,  # no decoy_value → editor chooses a plausible alternative
    )

    assert result.decoy_value == "teacher"
    assert editor.calls[0]["decoy_value"] is None


def test_build_decoy_prompt_carries_spans_attribute_and_optional_value() -> None:
    with_value = build_decoy_prompt("I work at Acme.", ["work at Acme"], "occupation", "nurse")
    assert "occupation" in with_value
    assert "work at Acme" in with_value
    assert "<decoy_value>nurse</decoy_value>" in with_value

    without_value = build_decoy_prompt("I work at Acme.", ["work at Acme"], "occupation", None)
    assert "decoy_value" not in without_value  # omitted when the model should choose


def test_decoy_version_pins_the_editor_slot() -> None:
    # decoy runs on the editor (`anonymizer`) slot — the separation chain keeps it distinct from the
    # proving adversary, so M3.7 proves the decoy against a model it cannot game.
    assert DECOY_VERSION.endswith("@anonymizer")
