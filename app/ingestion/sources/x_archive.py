"""X (Twitter) archive adapter (upload-x-archive.md).

Parses ``data/tweets.js`` (a JSON array behind a ``window.YTD.tweets.partN =`` assignment) into
`ParsedTextRecord`s. The archive is the subject's own, but a retweet (``RT @…``) is entirely
third-party content → `is_subject_authored=False` (dropped by the M1.2 gate). method=upload,
platform=x. (Quote tweets carry only the user's own added text in ``full_text`` → authored.)
"""

import io
import json
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from app.ingestion.base import Method, ParsedTextRecord, Platform

_TWEETS_FILE = "tweets.js"
_X_DATE_FORMAT = "%a %b %d %H:%M:%S %z %Y"  # e.g. "Wed Jan 15 12:34:56 +0000 2023"


def _parse_date(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _X_DATE_FORMAT)
    except ValueError:
        return None


def _strip_js_wrapper(raw: str) -> list[dict[str, Any]]:
    """Drop the ``window.YTD.tweets.partN =`` prefix and parse the JSON array."""
    equals = raw.find("=")
    payload = raw[equals + 1 :] if equals != -1 else raw
    parsed: list[dict[str, Any]] = json.loads(payload)
    return parsed


class XArchiveAdapter:
    """Reads an X archive zip; yields one record per tweet (retweets marked third-party)."""

    platform: Platform = "x"
    method: Method = "upload"

    def __init__(self, archive: bytes) -> None:
        self._archive = archive

    def parse(self) -> Iterable[ParsedTextRecord]:
        return list(self._records())

    def _records(self) -> Iterator[ParsedTextRecord]:
        with zipfile.ZipFile(io.BytesIO(self._archive)) as archive:
            name = next((n for n in archive.namelist() if n.endswith(_TWEETS_FILE)), None)
            if name is None:
                return
            raw = archive.read(name).decode("utf-8")
        for entry in _strip_js_wrapper(raw):
            tweet = entry.get("tweet", entry)
            text: str = tweet.get("full_text") or tweet.get("text") or ""
            if not text.strip():
                continue
            yield ParsedTextRecord(
                text=text,
                posted_at=_parse_date(tweet.get("created_at", "")),
                is_subject_authored=not text.startswith("RT @"),
            )
