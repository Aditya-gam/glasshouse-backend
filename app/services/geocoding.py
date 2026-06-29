"""Geo resolution — the `geo_hier` half of the normalizer (output-schema.md §5.3, §6).

The pure normalizer (`app.domain.normalize`) splits a place string into a best-effort
city/region/country. Resolving it to a real GeoNames entry (`geonames_id`, a trustworthy
`precision_level`) is **IO**, so it lives here behind a `Geocoder` port: the real adapter calls
GeoNames via `geopy`; CI and local dev inject `NullGeocoder`. `enrich_geo` runs at the service
layer, after normalization, keeping the domain core IO-free.

Fail-closed policy (chosen for a privacy product): on any miss/error/outage the heuristic split is
**kept** (`geonames_id` stays null) rather than dropped — never under-report location exposure
because a free external API blipped. The resolved place is **never logged** (it is inferred PII).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.attributes import BY_CODE
from app.domain.output_schema import AttributeGuess, GeoHierValue

logger = logging.getLogger(__name__)

PrecisionLevel = Literal["country", "region", "city", "neighborhood"]


@dataclass(frozen=True)
class GeoResolution:
    """A resolved GeoNames hit — the fields `enrich_geo` merges onto the heuristic value."""

    geonames_id: int
    precision_level: PrecisionLevel
    country: str | None = None
    region: str | None = None
    city: str | None = None
    neighborhood: str | None = None


class Geocoder(Protocol):
    """What the normalizer needs: free-text place -> a resolved hit, or None on a miss."""

    async def resolve(self, place: str) -> GeoResolution | None: ...


class NullGeocoder:
    """No-op geocoder (no GEONAMES_USERNAME / CI): every place misses -> heuristic split kept."""

    async def resolve(self, place: str) -> GeoResolution | None:
        return None


def _precision_from_fcode(fcode: str) -> PrecisionLevel:
    """Map a GeoNames feature code to our precision ladder (output-schema.md §5.3)."""
    if fcode.startswith("PCL"):  # political entity — a country
        return "country"
    if fcode.startswith("ADM"):  # administrative division — state/region
        return "region"
    if fcode == "PPLX":  # section of a populated place — a neighborhood
        return "neighborhood"
    return "city"  # any other populated place (PPL/PPLA/PPLC/…) — a city


def _to_resolution(raw: dict[str, object]) -> GeoResolution | None:
    """Build a `GeoResolution` from GeoNames' `searchJSON` payload (geopy `Location.raw`)."""
    geonames_id = raw.get("geonameId")
    if not isinstance(geonames_id, int | str):  # absent or an unexpected shape → a miss
        return None
    gid = int(geonames_id)
    precision = _precision_from_fcode(str(raw.get("fcode") or ""))
    name = str(raw.get("name") or "") or None
    country = str(raw.get("countryCode") or raw.get("countryName") or "") or None
    region = str(raw.get("adminName1") or "") or None
    if precision == "country":
        return GeoResolution(gid, "country", country=country)
    if precision == "region":
        return GeoResolution(gid, "region", country=country, region=name or region)
    return GeoResolution(
        gid,
        precision,
        country=country,
        region=region,
        city=name if precision == "city" else None,
        neighborhood=name if precision == "neighborhood" else None,
    )


class GeopyGeoNamesGeocoder:
    """GeoNames adapter. `geopy`'s client is synchronous, so the call is run off the event loop."""

    def __init__(self, username: str, *, timeout: float = 5.0) -> None:
        from geopy.geocoders import GeoNames

        self._client = GeoNames(username=username, timeout=timeout)

    async def resolve(self, place: str) -> GeoResolution | None:
        from geopy.exc import GeopyError

        try:
            location = await asyncio.to_thread(self._client.geocode, place, exactly_one=True)
        except GeopyError as exc:  # quota/timeout/service error — degrade to the heuristic split
            logger.warning("geocoder unavailable: %s", type(exc).__name__)  # no place text (PII)
            return None
        if location is None:
            return None
        return _to_resolution(location.raw)


async def _resolve_value(
    value: GeoHierValue, geocoder: Geocoder, *, clamp_to_city: bool
) -> GeoHierValue:
    """Resolve one heuristic geo value via GeoNames; on a miss keep it unchanged (fail-closed)."""
    query = ", ".join(p for p in (value.city, value.region, value.country) if p)
    if not query:
        return value
    resolution = await geocoder.resolve(query)
    if resolution is None:
        return value  # keep the heuristic split (user-chosen policy); geonames_id stays null
    precision = resolution.precision_level
    neighborhood = resolution.neighborhood or value.neighborhood
    if clamp_to_city:  # birthplace hierarchy is {country, region, city} only (output-schema §5.3)
        neighborhood = None
        precision = "city" if precision == "neighborhood" else precision
    return GeoHierValue(
        country=resolution.country or value.country,
        region=resolution.region or value.region,
        city=resolution.city or value.city,
        neighborhood=neighborhood,
        precision_level=precision,
        geonames_id=resolution.geonames_id,
    )


async def enrich_geo(guess: AttributeGuess, geocoder: Geocoder) -> AttributeGuess:
    """Resolve a geo_hier guess's candidates through GeoNames; pass non-geo guesses through."""
    if BY_CODE[guess.attribute].value_type != "geo_hier" or not guess.candidates:
        return guess
    clamp = guess.attribute == "birthplace"
    candidates = []
    for candidate in guess.candidates:
        value = candidate.value
        if isinstance(value, GeoHierValue):
            value = await _resolve_value(value, geocoder, clamp_to_city=clamp)
        candidates.append(candidate.model_copy(update={"value": value}))
    return guess.model_copy(update={"candidates": candidates})


def default_geocoder() -> Geocoder:
    """Process-wide geocoder: the real GeoNames adapter when configured, else a no-op."""
    from app.core.config import get_geocoding_settings

    settings = get_geocoding_settings()
    if not settings.geonames_username:
        return NullGeocoder()
    return GeopyGeoNamesGeocoder(settings.geonames_username, timeout=settings.geonames_timeout)
