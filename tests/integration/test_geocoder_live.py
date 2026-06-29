"""Opt-in live GeoNames call — the one thing the fakes can't cover (the real geopy adapter +
payload parsing). Gated behind an explicit opt-in so normal local/CI runs stay network-free: a
placeholder GEONAMES_USERNAME in a dev `.env` must not turn this into a failing test. Run it with
``GEONAMES_LIVE_TEST=1`` once your GeoNames account has web services enabled."""

import os

import pytest

from app.core.config import get_geocoding_settings
from app.services.geocoding import GeopyGeoNamesGeocoder


@pytest.mark.skipif(
    not os.getenv("GEONAMES_LIVE_TEST"),
    reason="opt-in live test; set GEONAMES_LIVE_TEST=1 (needs a web-enabled GeoNames user)",
)
async def test_live_geonames_resolves_a_known_city() -> None:
    settings = get_geocoding_settings()
    assert settings.geonames_username, "GEONAMES_USERNAME must be set to run the live geocoder test"
    geocoder = GeopyGeoNamesGeocoder(settings.geonames_username, timeout=settings.geonames_timeout)

    resolution = await geocoder.resolve("Seattle, Washington, US")

    # Network output is not byte-stable; assert the contract (resolved id + a sane hierarchy).
    assert resolution is not None
    assert resolution.geonames_id == 5809844  # Seattle's stable GeoNames id
    assert resolution.precision_level in {"city", "neighborhood"}
    assert resolution.country == "US"
