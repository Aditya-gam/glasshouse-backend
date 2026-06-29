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


# --- hierarchical geo (M1.8b §3, §5) ---------------------------------------------------------
def _geo_full(
    country: str | None = None,
    region: str | None = None,
    city: str | None = None,
    neighborhood: str | None = None,
    *,
    precision: str,
    geonames_id: int | None = None,
) -> GeoHierValue:
    return GeoHierValue(
        country=country,
        region=region,
        city=city,
        neighborhood=neighborhood,
        precision_level=precision,  # type: ignore[arg-type]
        geonames_id=geonames_id,
    )


def test_geo_reports_finest_level_clearing_threshold() -> None:
    runs = [
        _run(
            "location",
            _geo_full(
                "US", "Washington", "Seattle", "Fremont", precision="neighborhood", geonames_id=5
            ),
        ),
        _run(
            "location", _geo_full("US", "Washington", "Seattle", precision="city", geonames_id=10)
        ),
        _run("location", _geo_full("US", "Oregon", "Portland", precision="city", geonames_id=20)),
    ]
    result = aggregate("location", runs, n_runs=3)
    top = result.candidates[0].value
    assert isinstance(top, GeoHierValue)
    assert (top.city, top.precision_level, top.neighborhood) == ("Seattle", "city", None)
    assert top.geonames_id == 10  # the city-precision member's id, not Fremont's
    assert result.candidates[0].confidence.raw == 2 / 3
    assert result.candidates[1].confidence.raw == 1 / 3  # Portland runner-up


def test_geo_neighborhood_clears_when_all_agree() -> None:
    value = _geo_full(
        "US", "Washington", "Seattle", "Fremont", precision="neighborhood", geonames_id=5
    )
    top = aggregate("location", [_run("location", value)] * 3, n_runs=3).candidates[0].value
    assert isinstance(top, GeoHierValue)
    assert top.precision_level == "neighborhood" and top.neighborhood == "Fremont"
    assert top.geonames_id == 5


def test_geo_falls_back_to_region_when_cities_differ() -> None:
    runs = [
        _run(
            "location", _geo_full("US", "Washington", "Seattle", precision="city", geonames_id=10)
        ),
        _run("location", _geo_full("US", "Washington", "Tacoma", precision="city", geonames_id=11)),
        _run("location", _geo_full("US", "Oregon", "Portland", precision="city", geonames_id=20)),
    ]
    top = aggregate("location", runs, n_runs=3).candidates[0].value
    assert isinstance(top, GeoHierValue)
    assert (top.region, top.city, top.precision_level) == ("Washington", None, "region")
    assert top.geonames_id is None  # no member resolved at region precision


def test_geo_abstains_when_even_country_disagrees() -> None:
    runs = [
        _run("location", _geo_full("US", precision="country", geonames_id=1)),
        _run("location", _geo_full("FR", precision="country", geonames_id=2)),
        _run("location", _geo_full("JP", precision="country", geonames_id=3)),
    ]
    assert aggregate("location", runs, n_runs=3).status == "abstained"


def test_birthplace_caps_at_city() -> None:
    value = _geo_full("PT", "Porto", "Porto", "Ribeira", precision="neighborhood", geonames_id=7)
    top = aggregate("birthplace", [_run("birthplace", value)] * 3, n_runs=3).candidates[0].value
    assert isinstance(top, GeoHierValue)
    assert top.precision_level == "city" and top.neighborhood is None


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
