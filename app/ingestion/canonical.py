"""The canonical text item — the normalized, source-agnostic unit (`canonical-item.md`).

Every adapter's output funnels to this shape so the attack/measure/defend engine runs identically
on uploads, connector pulls, and benchmark loaders. Pure data, no IO. Storage-bound fields
(`content_hmac`, `embedding`, the owning ids) are added at persist time (M1.3), not here.
"""

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ingestion.base import Platform


class CanonicalTextItem(BaseModel):
    """One normalized piece of the subject's own text content.

    Invariants: `text` is non-empty (whitespace-only records are dropped upstream) and `posted_at`,
    when present, is timezone-aware UTC — the original zone is preserved separately in `original_tz`
    as a location/routine signal.
    """

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    posted_at: datetime | None
    original_tz: str | None
    platform: Platform
    lang: str | None
    is_subject_authored: bool

    @field_validator("posted_at")
    @classmethod
    def _must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != timedelta(0):
            raise ValueError("posted_at must be timezone-aware UTC")
        return value
