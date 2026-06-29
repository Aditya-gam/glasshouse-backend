"""Unit (M1.7b): the service-layer geo enrichment over the `Geocoder` port.

`enrich_geo` resolves a geo_hier guess's heuristic split through GeoNames (here a fake): it fills
`geonames_id` + `precision_level`, keeps the heuristic split on a miss (fail-closed), clamps
birthplace to city, and passes non-geo guesses through untouched. No network.
"""

from app.domain.normalize import normalize_guess
from app.domain.output_schema import (
    AttributeCode,
    GeoHierValue,
    RawAttributeGuess,
    RawCandidate,
)
from app.services.geocoding import (
    GeoResolution,
    NullGeocoder,
    _to_resolution,
    enrich_geo,
)


class _FakeGeocoder:
    """Returns a fixed resolution for every place (or None to simulate a miss)."""

    def __init__(self, resolution: GeoResolution | None) -> None:
        self._resolution = resolution

    async def resolve(self, place: str) -> GeoResolution | None:
        return self._resolution


def _geo_guess(attribute: AttributeCode, value_text: str) -> RawAttributeGuess:
    return RawAttributeGuess(
        attribute=attribute,
        status="inferred",
        candidates=[RawCandidate(value_text=value_text, self_confidence=0.8)],
    )


async def test_enrich_fills_geonames_resolution() -> None:
    guess = normalize_guess(_geo_guess("location", "Seattle, WA"))
    geocoder = _FakeGeocoder(
        GeoResolution(
            5809844,
            "neighborhood",
            country="US",
            region="Washington",
            city="Seattle",
            neighborhood="Fremont",
        )
    )
    value = (await enrich_geo(guess, geocoder)).candidates[0].value
    assert isinstance(value, GeoHierValue)
    assert value.geonames_id == 5809844 and value.precision_level == "neighborhood"
    assert (value.city, value.region, value.country) == ("Seattle", "Washington", "US")
    assert value.neighborhood == "Fremont"


async def test_enrich_miss_keeps_heuristic_split() -> None:
    guess = normalize_guess(_geo_guess("location", "Atlantis, Nowhere"))
    before = guess.candidates[0].value
    assert isinstance(before, GeoHierValue)
    value = (await enrich_geo(guess, NullGeocoder())).candidates[0].value
    assert isinstance(value, GeoHierValue)
    assert value.geonames_id is None and value.city == before.city  # heuristic preserved


async def test_birthplace_clamps_neighborhood_to_city() -> None:
    guess = normalize_guess(_geo_guess("birthplace", "Porto, Portugal"))
    geocoder = _FakeGeocoder(
        GeoResolution(
            2735943,
            "neighborhood",
            country="PT",
            region="Porto",
            city="Porto",
            neighborhood="Ribeira",
        )
    )
    value = (await enrich_geo(guess, geocoder)).candidates[0].value
    assert isinstance(value, GeoHierValue)
    assert value.precision_level == "city" and value.neighborhood is None
    assert value.geonames_id == 2735943


async def test_non_geo_guess_passes_through_untouched() -> None:
    guess = normalize_guess(
        RawAttributeGuess(
            attribute="age",
            status="inferred",
            candidates=[RawCandidate(value_text="31", self_confidence=0.8)],
        )
    )
    assert await enrich_geo(guess, _FakeGeocoder(GeoResolution(1, "city"))) is guess


async def test_abstained_guess_passes_through_untouched() -> None:
    guess = normalize_guess(RawAttributeGuess(attribute="location", status="abstained"))
    assert await enrich_geo(guess, _FakeGeocoder(GeoResolution(1, "city"))) is guess


def test_to_resolution_maps_feature_code_to_precision() -> None:
    city = _to_resolution(
        {
            "geonameId": 1,
            "fcode": "PPL",
            "name": "Seattle",
            "countryCode": "US",
            "adminName1": "Washington",
        }
    )
    region = _to_resolution(
        {"geonameId": 2, "fcode": "ADM1", "name": "Washington", "countryCode": "US"}
    )
    country = _to_resolution(
        {"geonameId": 3, "fcode": "PCLI", "name": "United States", "countryCode": "US"}
    )
    hood = _to_resolution(
        {
            "geonameId": 4,
            "fcode": "PPLX",
            "name": "Fremont",
            "countryCode": "US",
            "adminName1": "Washington",
        }
    )
    assert city is not None and city.precision_level == "city" and city.city == "Seattle"
    assert (
        region is not None and region.precision_level == "region" and region.region == "Washington"
    )
    assert country is not None and country.precision_level == "country" and country.country == "US"
    assert (
        hood is not None
        and hood.precision_level == "neighborhood"
        and hood.neighborhood == "Fremont"
    )


def test_to_resolution_without_geoname_id_is_none() -> None:
    assert _to_resolution({"fcode": "PPL", "name": "Nowhere"}) is None
