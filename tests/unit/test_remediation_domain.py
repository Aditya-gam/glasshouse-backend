"""Unit (M3.7): the pure remediation helpers — majority recovery + action classification."""

from app.domain.remediation import classify_action, majority_recovered, span_change


def test_majority_recovered_needs_at_least_half() -> None:
    assert majority_recovered([True, True, False]) is True  # 2/3
    assert majority_recovered([True, False]) is True  # tie → fail closed toward "still exposed"
    assert majority_recovered([True, False, False]) is False  # 1/3
    assert majority_recovered([]) is False  # no evidence of recovery


def test_classify_action_remove_only_when_every_op_removes_the_item() -> None:
    assert classify_action(["remove_item", "remove_item"]) == "remove"
    assert classify_action(["generalize"]) == "rewrite"
    assert classify_action(["generalize", "remove_item"]) == "rewrite"  # a mix keeps content
    assert classify_action([]) == "rewrite"  # nothing removed → not a removal


def test_span_change_drops_replacement_only_on_whole_item_removal() -> None:
    generalized = span_change("a", "generalize", "I live near a park")
    assert generalized.replacement == "I live near a park"

    removed = span_change("b", "remove_item", "whatever the model returned")
    assert removed.replacement is None  # a removed item carries no replacement text
