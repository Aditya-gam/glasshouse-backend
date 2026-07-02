"""Ingestion service — the shared steps that turn parsed records into canonical items.

`run_ingestion` runs the uniform pipeline: per-source parse (the adapter) → normalize →
third-party drop (rule 5). Encrypt + `content_hmac` dedupe + embed + persist (M1.3) layer on
after this returns. No content is logged (rule 1).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import py3langid as langid
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ingestion.base import Method, ParsedTextRecord, Platform, SourceAdapter
from app.ingestion.canonical import CanonicalTextItem
from app.repositories import import_sources as import_sources_repo
from app.repositories import items as items_repo
from app.repositories import profiles as profiles_repo
from app.retrieval.embedder import Embedder


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


def drop_third_party(items: Iterable[CanonicalTextItem]) -> list[CanonicalTextItem]:
    """Discard content the subject did not author (rule 5), before encrypt/embed/persist.

    Fail-closed: only items explicitly marked `is_subject_authored` survive — uncertain authorship
    is set false by the adapter and dropped here, so third-party content never reaches storage or
    the embedding index (third-party-drop.md). Intra-item quote scrubbing is per-source (M1.4).
    """
    return [item for item in items if item.is_subject_authored]


def run_ingestion(adapter: SourceAdapter) -> list[CanonicalTextItem]:
    """Parse → normalize → drop third-party: the subject's own canonical items, ready for M1.3."""
    normalized = (normalize(record, platform=adapter.platform) for record in adapter.parse())
    canonical = [item for item in normalized if item is not None]
    return drop_third_party(canonical)


@dataclass(frozen=True)
class PersistResult:
    """Outcome of persisting one ingestion run (counts only — never content)."""

    import_source_id: UUID | None
    inserted: int
    deduped: int


async def persist_items(
    conn: AsyncConnection,
    embedder: Embedder,
    *,
    owner_user_id: UUID,
    items: list[CanonicalTextItem],
    method: Method,
    master_key: str,
    profile_id: UUID | None = None,
    import_source_id: UUID | None = None,
) -> PersistResult:
    """Embed + encrypt + dedupe-persist the subject's canonical items (M1.3), RLS-scoped to owner.

    Assumes the third-party drop already ran (`run_ingestion`). Items land on `profile_id` when
    given (the benchmark seed's per-persona profiles) or the owner's `self` profile. Provenance:
    a fresh `import_source` per call (one row per import event), unless the caller passes a
    deterministic `import_source_id` (the idempotent benchmark seed). Re-imports of the same
    content into the same profile are skipped (keyed-HMAC dedupe). No content is logged.
    """
    if not items:
        return PersistResult(import_source_id=None, inserted=0, deduped=0)
    if profile_id is None:
        profile_id = await profiles_repo.get_or_create_self_profile(conn, owner_user_id)
    if import_source_id is None:
        import_source_id = await import_sources_repo.create_import_source(
            conn, profile_id, platform=items[0].platform, method=method
        )
    else:
        await import_sources_repo.ensure_import_source(
            conn, import_source_id, profile_id, platform=items[0].platform, method=method
        )
    vectors = embedder.embed([item.text for item in items])
    inserted = 0
    for item, vector in zip(items, vectors, strict=True):
        item_id = await items_repo.insert_canonical_item(
            conn,
            profile_id=profile_id,
            owner_user_id=owner_user_id,
            import_source_id=import_source_id,
            plaintext=item.text,
            embedding=vector,
            posted_at=item.posted_at,
            original_tz=item.original_tz,
            is_subject_authored=item.is_subject_authored,
            master_key=master_key,
        )
        if item_id is not None:
            inserted += 1
    return PersistResult(
        import_source_id=import_source_id, inserted=inserted, deduped=len(items) - inserted
    )


async def ingest_and_persist(
    conn: AsyncConnection,
    embedder: Embedder,
    adapter: SourceAdapter,
    *,
    owner_user_id: UUID,
    master_key: str,
    profile_id: UUID | None = None,
    import_source_id: UUID | None = None,
) -> PersistResult:
    """Full ingestion (M1.1–M1.3): parse → normalize → drop → embed + encrypt + dedupe-persist."""
    items = run_ingestion(adapter)
    return await persist_items(
        conn,
        embedder,
        owner_user_id=owner_user_id,
        items=items,
        method=adapter.method,
        master_key=master_key,
        profile_id=profile_id,
        import_source_id=import_source_id,
    )
