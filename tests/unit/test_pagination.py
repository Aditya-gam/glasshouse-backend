"""Unit (M5.2): the opaque pagination cursor codec — round-trip, opacity, tamper rejection."""

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest

from app.api.v1.pagination import InvalidCursor, decode_cursor, encode_cursor


def test_cursor_round_trips_the_key() -> None:
    ts = datetime(2026, 5, 1, 12, 30, 45, tzinfo=UTC)
    row_id = uuid.uuid4()

    decoded_ts, decoded_id = decode_cursor(encode_cursor(ts, row_id))

    assert decoded_ts == ts
    assert decoded_id == row_id


def test_cursor_is_opaque_and_url_safe() -> None:
    cursor = encode_cursor(datetime(2026, 5, 1, tzinfo=UTC), uuid.uuid4())

    assert "2026" not in cursor  # the raw timestamp does not leak through
    assert all(ch.isalnum() or ch in "-_=" for ch in cursor)  # base64url alphabet only


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "not base64!!",  # not base64 at all
        "YWJj",  # base64 of "abc" — not JSON
        "eyJ0cyI6ICJub3QtYS1kYXRlIiwgImlkIjogIngifQ==",  # JSON, but ts/id unparseable
    ],
)
def test_malformed_cursor_raises(bad: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_non_string_id_raises_not_a_500() -> None:
    # a tampered cursor whose JSON `id` is a non-string (UUID() would AttributeError on it) must
    # surface as InvalidCursor (→ 422), never escape as an unhandled error.
    forged = base64.urlsafe_b64encode(
        json.dumps({"ts": "2026-05-01T12:30:45+00:00", "id": 123}).encode()
    ).decode()

    with pytest.raises(InvalidCursor):
        decode_cursor(forged)
