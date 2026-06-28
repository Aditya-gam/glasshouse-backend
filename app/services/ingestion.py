"""Ingestion service — the shared steps that turn parsed records into canonical items.

`run_ingestion` is the parsed → canonical seam (M1.1): per-source parsing (the adapter) feeds the
uniform normalize step. The third-party DROP (M1.2) and encrypt + `content_hmac` dedupe + embed +
persist (M1.3) layer on after this returns. No content is logged (rule 1).
"""

from datetime import UTC, datetime

import py3langid as langid

from app.ingestion.base import ParsedTextRecord, Platform, SourceAdapter
from app.ingestion.canonical import CanonicalTextItem


def _offset_label(dt: datetime) -> str | None:
    """The datetime's UTC offset as a stable ``±HH:MM`` label (a location/routine signal)."""
    offset = dt.utcoffset()
    if offset is None:
        return None
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _to_utc(posted_at: datetime | None) -> tuple[datetime | None, str | None]:
    """Resolve a record timestamp to (UTC instant, original-zone label).

    Naive timestamps are assumed UTC with no recoverable original zone.
    """
    if posted_at is None:
        return None, None
    if posted_at.tzinfo is None:
        return posted_at.replace(tzinfo=UTC), None
    return posted_at.astimezone(UTC), _offset_label(posted_at)


def _detect_lang(text: str) -> str:
    """ISO 639-1 language code for the text (deterministic, offline)."""
    code, _score = langid.classify(text)
    return str(code)


def normalize(record: ParsedTextRecord, *, platform: Platform) -> CanonicalTextItem | None:
    """Normalize one parsed record to a canonical item, or None if it has no content.

    Strips surrounding whitespace, resolves the timestamp to UTC (keeping the original zone),
    and detects the language when the source didn't declare one.
    """
    text = record.text.strip()
    if not text:
        return None
    posted_at, original_tz = _to_utc(record.posted_at)
    return CanonicalTextItem(
        text=text,
        posted_at=posted_at,
        original_tz=original_tz,
        platform=platform,
        lang=record.lang or _detect_lang(text),
        is_subject_authored=record.is_subject_authored,
    )


def run_ingestion(adapter: SourceAdapter) -> list[CanonicalTextItem]:
    """Parse a source and normalize it to canonical text items (parsed → canonical)."""
    normalized = (normalize(record, platform=adapter.platform) for record in adapter.parse())
    return [item for item in normalized if item is not None]
