"""Unit (M2.1): the pure SynthPAI parser — grouping, label mapping, review aggregation."""

from typing import Any

from app.ingestion.sources.synthpai import parse_synthpai_rows

_PROFILE: dict[str, Any] = {
    "age": 55,
    "sex": "male",
    "city_country": "Montreal, Canada",
    "birth_city_country": "Ankara, Turkey",
    "education": "Bachelors in Business Administration",
    "occupation": "financial manager",
    "income": "90 thousand canadian dollars",
    "income_level": "middle",
    "relationship_status": "divorced",
    "style": "generation metadata — must be ignored",
}


def _row(
    author: str,
    text: str,
    profile: dict[str, Any] | None = None,
    reviews: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "author": author,
        "username": f"user-{author}",
        "profile": _PROFILE if profile is None else profile,
        "text": text,
        "guesses": None,
        "reviews": {"human": reviews or {}},
        "id": "x",
        "parent_id": None,
        "thread_id": "t",
        "children": [],
    }


def test_groups_rows_by_author_in_first_seen_order() -> None:
    rows = [_row("a", "one"), _row("b", "three"), _row("a", "two")]

    personas = parse_synthpai_rows(rows)

    assert [p.author for p in personas] == ["a", "b"]
    assert [r.text for r in personas[0].records] == ["one", "two"]
    assert all(r.is_subject_authored for p in personas for r in p.records)


def test_maps_profile_keys_to_attribute_codes() -> None:
    personas = parse_synthpai_rows([_row("a", "hello")])

    labels = personas[0].labels
    assert set(labels) == {
        "age",
        "sex",
        "location",
        "birthplace",
        "occupation",
        "education",
        "relationship",
        "income",
    }
    assert labels["age"].value == 55
    assert labels["location"].value == "Montreal, Canada"
    assert labels["birthplace"].value == "Ankara, Turkey"
    assert labels["income"].value == "middle"  # income_level, not the freeform income string
    assert labels["relationship"].value == "divorced"


def test_aggregates_reviews_min_hardness_max_certainty() -> None:
    rows = [
        _row("a", "one", reviews={"city_country": {"estimate": "", "hardness": 3, "certainty": 2}}),
        _row("a", "two", reviews={"city_country": {"estimate": "", "hardness": 1, "certainty": 3}}),
        _row("a", "три", reviews={"city_country": {"estimate": "", "hardness": 0, "certainty": 0}}),
    ]

    labels = parse_synthpai_rows(rows)[0].labels

    # hardness 0 belongs to a non-revealing review (certainty 0) and must not win the min.
    assert labels["location"].hardness == 1
    assert labels["location"].certainty == 3


def test_hardness_zero_sentinel_never_wins_even_when_revealing() -> None:
    """Real SynthPAI rows leave hardness at the 0 sentinel beside certainty > 0 — the graded
    scale is 1–5, so 0 must not collapse the min (it would pollute by-hardness eval buckets)."""
    rows = [
        _row("a", "one", reviews={"education": {"estimate": "", "hardness": 0, "certainty": 5}}),
        _row("a", "two", reviews={"education": {"estimate": "", "hardness": 4, "certainty": 2}}),
        _row("b", "solo", reviews={"education": {"estimate": "", "hardness": 0, "certainty": 5}}),
    ]

    personas = {p.author: p for p in parse_synthpai_rows(rows)}

    assert personas["a"].labels["education"].hardness == 4  # the sentinel didn't win the min
    assert personas["a"].labels["education"].certainty == 5
    # revealed (certainty > 0) but only sentinel hardness → ungraded, not hardness 0.
    assert personas["b"].labels["education"].hardness is None
    assert personas["b"].labels["education"].certainty == 5


def test_unrevealed_attribute_has_no_hardness_and_zero_certainty() -> None:
    labels = parse_synthpai_rows([_row("a", "hello")])[0].labels

    assert labels["sex"].hardness is None
    assert labels["sex"].certainty == 0


def test_skips_rows_without_author_or_text_and_empty_profile_values() -> None:
    sparse_profile = {**_PROFILE, "occupation": "  ", "age": None}
    rows = [
        _row("a", "kept", profile=sparse_profile),
        _row("a", "   "),  # blank text → skipped
        {"text": "no author", "profile": _PROFILE},  # no author → skipped
    ]

    personas = parse_synthpai_rows(rows)

    assert len(personas) == 1 and len(personas[0].records) == 1
    assert "occupation" not in personas[0].labels and "age" not in personas[0].labels
