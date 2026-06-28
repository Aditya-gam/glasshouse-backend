"""Reddit 'request my data' export adapter (upload-reddit-export.md).

Parses ``comments.csv`` + ``posts.csv`` from the export zip into `ParsedTextRecord`s. The whole
export is the subject's own content → `is_subject_authored=True` (the pipeline still runs the drop
gate). Conforms to the `SourceAdapter` port; method=upload, platform=reddit.
"""

import csv
import io
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from app.ingestion.base import Method, ParsedTextRecord, Platform


def _parse_date(raw: str) -> datetime | None:
    """Reddit stamps are ``2023-01-15 12:34:56 UTC`` (or ISO); best-effort → tz-aware UTC."""
    cleaned = raw.strip().removesuffix(" UTC").strip()
    if not cleaned:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class RedditExportAdapter:
    """Reads a Reddit export zip; yields one record per comment and post."""

    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, archive: bytes) -> None:
        self._archive = archive

    def parse(self) -> Iterable[ParsedTextRecord]:
        return list(self._records())

    def _records(self) -> Iterator[ParsedTextRecord]:
        with zipfile.ZipFile(io.BytesIO(self._archive)) as archive:
            yield from self._rows(archive, "comments.csv", text_fields=("body",))
            yield from self._rows(archive, "posts.csv", text_fields=("title", "body"))

    def _rows(
        self, archive: zipfile.ZipFile, suffix: str, *, text_fields: tuple[str, ...]
    ) -> Iterator[ParsedTextRecord]:
        name = next((n for n in archive.namelist() if n.endswith(suffix)), None)
        if name is None:
            return
        with archive.open(name) as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8"))
            for row in reader:
                text = " ".join(part for field in text_fields if (part := row.get(field, "")))
                if not text.strip():
                    continue
                yield ParsedTextRecord(text=text, posted_at=_parse_date(row.get("date", "")))
