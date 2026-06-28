"""The source-adapter port + its raw output (`03-data/ingestion/overview.md`, `sources/*`).

A `SourceAdapter` is the hexagonal port the ingestion service depends on: each per-source
adapter (upload/connector/loader, M1.4+) parses its format and determines authorship, emitting
raw `ParsedTextRecord`s. The shared, uniform `normalize` step (service layer) turns those into
canonical items — so no adapter can skip or diverge from normalization.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

# Provenance vocabularies — mirror the import_sources CHECK constraints (import_sources.md).
Platform = Literal["reddit", "mastodon", "x", "google", "linkedin", "photos", "synthpai", "vip"]
Method = Literal["upload", "connector", "loader"]


class ParsedTextRecord(BaseModel):
    """One raw text record straight from an adapter — pre-normalization, pre-drop, pre-encrypt.

    `posted_at` may be timezone-aware (preferred), naive, or absent; `normalize` resolves it to
    UTC. `lang` is set only when the source itself declares it; otherwise the service detects it.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    posted_at: datetime | None = None
    is_subject_authored: bool = True
    lang: str | None = None


class SourceAdapter(Protocol):
    """What the ingestion service needs of a source: its provenance + a parse pass.

    Structural (duck-typed) — adapters conform by shape, no base class required.
    """

    platform: Platform
    method: Method

    def parse(self) -> Iterable[ParsedTextRecord]: ...
