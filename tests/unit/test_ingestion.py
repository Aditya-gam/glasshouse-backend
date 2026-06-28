"""Unit (M1.1): the ingestion seam turns parsed records into canonical items (parsed → canonical).

Pure — a fake in-memory adapter stands in for the real upload/connector adapters (M1.4+).
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.ingestion.canonical import CanonicalTextItem
from app.services.ingestion import run_ingestion

_IST = timezone(timedelta(hours=5, minutes=30))


class _FakeAdapter:
    """A minimal SourceAdapter; conforms to the port by shape (no base class)."""

    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


def test_parsed_records_become_canonical_items() -> None:
    records = [
        ParsedTextRecord(
            text="  I went hiking near Gas Works Park in Seattle this weekend.  ",
            posted_at=datetime(2026, 6, 1, 9, 30, tzinfo=_IST),
        ),
        ParsedTextRecord(text="   \n  "),  # whitespace-only → dropped
        ParsedTextRecord(text="No timestamp here, but clearly an English sentence to detect."),
    ]

    items = run_ingestion(_FakeAdapter(records))

    assert len(items) == 2  # the whitespace-only record produced no canonical item
    assert all(isinstance(i, CanonicalTextItem) for i in items)

    first = items[0]
    assert first.text == "I went hiking near Gas Works Park in Seattle this weekend."  # stripped
    assert first.posted_at == datetime(2026, 6, 1, 4, 0, tzinfo=UTC)  # 09:30+05:30 → 04:00Z
    assert first.posted_at is not None and first.posted_at.utcoffset() == timedelta(0)
    assert first.original_tz == "+05:30"  # original zone kept as a signal
    assert first.platform == "reddit"
    assert first.lang == "en"
    assert first.is_subject_authored is True


def test_authorship_flag_is_preserved_not_dropped() -> None:
    # M1.1 carries is_subject_authored through unchanged; the third-party DROP is M1.2.
    records = [
        ParsedTextRecord(
            text="A third party wrote this English sentence.", is_subject_authored=False
        )
    ]
    items = run_ingestion(_FakeAdapter(records))
    assert len(items) == 1
    assert items[0].is_subject_authored is False


def test_language_is_detected_per_item() -> None:
    items = run_ingestion(
        _FakeAdapter(
            [
                ParsedTextRecord(
                    text="Hola, me llamo Juan y vivo en Madrid, soy ingeniero de software."
                )
            ]
        )
    )
    assert items[0].lang == "es"


def test_source_declared_language_is_respected() -> None:
    items = run_ingestion(_FakeAdapter([ParsedTextRecord(text="ambiguous text", lang="fr")]))
    assert items[0].lang == "fr"  # the source's own label wins over detection


def test_naive_timestamp_assumed_utc_with_no_original_zone() -> None:
    items = run_ingestion(
        _FakeAdapter(
            [
                ParsedTextRecord(
                    text="Naive timestamp English sentence for the test.",
                    posted_at=datetime(2026, 6, 1, 9, 30),  # naive
                )
            ]
        )
    )
    item = items[0]
    assert item.posted_at == datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
    assert item.original_tz is None


def test_canonical_rejects_non_utc_posted_at() -> None:
    with pytest.raises(ValidationError):
        CanonicalTextItem(
            text="x",
            posted_at=datetime(2026, 6, 1, 9, 30, tzinfo=_IST),  # not UTC
            original_tz="+05:30",
            platform="reddit",
            lang="en",
            is_subject_authored=True,
        )
