"""Unit (M1.8a): self-consistency aggregation — N per-run guesses → one consensus.

Example tests pin the worked cases (confidence doc §4-§6); Hypothesis property tests assert the
structural invariants of the agreement-fraction signal hold for any ensemble.
"""

from math import ceil

from hypothesis import given
from hypothesis import strategies as st

from app.domain.consistency import aggregate
from app.domain.output_schema import (
    AttributeCode,
    AttributeGuess,
    AttributeValue,
    Candidate,
    CategoricalValue,
    Confidence,
    GeoHierValue,
    NumericValue,
)

_SEX = ("male", "female", "non-binary", "other", "unknown")


def _run(
    attribute: AttributeCode, value: AttributeValue, *, self_conf: float = 0.8
) -> AttributeGuess:
    return AttributeGuess(
        attribute=attribute,
        modality="text",
        status="inferred",
        candidates=[
            Candidate(
                rank=1,
                value=value,
                confidence=Confidence(
                    raw=self_conf, source="self_reported", self_reported=self_conf
                ),
            )
        ],
        reasoning="r",
    )


def _abstain(attribute: AttributeCode) -> AttributeGuess:
    return AttributeGuess(attribute=attribute, modality="text", status="abstained", candidates=[])


def _cat(value: str) -> CategoricalValue:
    return CategoricalValue(value=value)


def _num(estimate: float) -> NumericValue:
    return NumericValue(estimate=estimate)


def _inc(bracket: str) -> NumericValue:
    return NumericValue(estimate=95000.0, bracket=bracket, unit="USD/yr")  # type: ignore[arg-type]


def _geo(geonames_id: int) -> GeoHierValue:
    return GeoHierValue(city="X", precision_level="city", geonames_id=geonames_id)


# --- example cases ---------------------------------------------------------------------------
def test_unanimous_agreement_is_raw_one() -> None:
    result = aggregate("sex", [_run("sex", _cat("male"))] * 3, n_runs=3)
    assert result.status == "inferred" and len(result.candidates) == 1
    top = result.candidates[0]
    assert top.confidence.raw == 1.0 and top.confidence.source == "self_consistency"
    assert top.confidence.agreement is not None and top.confidence.agreement.n_agree == 3


def test_majority_split_ranks_runner_up() -> None:
    guesses = [_run("sex", _cat("male")), _run("sex", _cat("male")), _run("sex", _cat("female"))]
    result = aggregate("sex", guesses, n_runs=3)
    assert [c.value for c in result.candidates] == [_cat("male"), _cat("female")]
    assert result.candidates[0].confidence.raw == 2 / 3
    assert result.candidates[1].confidence.raw == 1 / 3


def test_no_plurality_abstains() -> None:
    guesses = [_run("sex", _cat("male")), _run("sex", _cat("female")), _run("sex", _cat("other"))]
    result = aggregate("sex", guesses, n_runs=3)
    assert result.status == "abstained" and result.candidates == []


def test_omitted_runs_count_against_agreement() -> None:
    # one run infers, two omit/abstain → 1/3 is below the ⌈3/2⌉=2 plurality floor → abstain.
    guesses = [_run("location", _geo(5809844)), _abstain("location"), _abstain("location")]
    result = aggregate("location", guesses, n_runs=3)
    assert result.status == "abstained"


def test_age_clusters_within_tolerance_band() -> None:
    guesses = [_run("age", _num(28)), _run("age", _num(30)), _run("age", _num(41))]
    result = aggregate("age", guesses, n_runs=3)  # 28≈30 (±3), 41 apart
    assert result.candidates[0].confidence.raw == 2 / 3
    assert isinstance(result.candidates[0].value, NumericValue)


def test_income_clusters_by_bracket() -> None:
    guesses = [
        _run("income", _inc("high")),
        _run("income", _inc("high")),
        _run("income", _inc("low")),
    ]
    result = aggregate("income", guesses, n_runs=3)
    assert result.candidates[0].confidence.raw == 2 / 3
    value = result.candidates[0].value
    assert isinstance(value, NumericValue) and value.bracket == "high"


def test_geo_clusters_by_geonames_id() -> None:
    guesses = [
        _run("location", _geo(5809844)),
        _run("location", _geo(5809844)),
        _run("location", _geo(5746545)),
    ]
    result = aggregate("location", guesses, n_runs=3)
    assert result.candidates[0].confidence.raw == 2 / 3


# --- property tests (invariants of the signal) -----------------------------------------------
@st.composite
def _sex_ensemble(draw: st.DrawFn) -> tuple[int, list[AttributeGuess]]:
    n_runs = draw(st.integers(min_value=2, max_value=6))
    n_answered = draw(st.integers(min_value=0, max_value=n_runs))
    guesses = [
        _run("sex", _cat(draw(st.sampled_from(_SEX))), self_conf=draw(st.floats(0, 1)))
        for _ in range(n_answered)
    ]
    return n_runs, guesses


@given(_sex_ensemble())
def test_aggregate_invariants(case: tuple[int, list[AttributeGuess]]) -> None:
    n_runs, guesses = case
    result = aggregate("sex", guesses, n_runs=n_runs)
    assert result.status in {"inferred", "abstained"}
    if result.status == "abstained":
        assert result.candidates == []
        return
    raws = [c.confidence.raw for c in result.candidates]
    assert all(0.0 <= r <= 1.0 for r in raws)
    assert raws == sorted(raws, reverse=True)  # candidates ranked by agreement, descending
    assert [c.rank for c in result.candidates] == list(range(1, len(result.candidates) + 1))
    agreements = [c.confidence.agreement for c in result.candidates]
    assert all(a is not None for a in agreements)
    top = agreements[0]
    assert top is not None
    assert result.candidates[0].confidence.raw == top.n_agree / n_runs
    assert top.n_agree >= ceil(n_runs / 2)  # cleared the plurality floor
    assert sum(a.n_agree for a in agreements if a is not None) <= n_runs
