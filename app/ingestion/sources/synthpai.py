"""SynthPAI benchmark parser (loader-synthpai.md, M2.1) — pure, no IO.

Parses the published `RobinSta/SynthPAI` JSONL rows (one row per synthetic comment) into
per-persona records + ground-truth labels. Each row carries the full persona `profile` (the
labels) plus per-comment human reviews (`hardness`/`certainty` of inferring each attribute from
that comment); we aggregate reviews to the persona level. Fully synthetic — no data subject.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.output_schema import AttributeCode
from app.ingestion.base import Method, ParsedTextRecord, Platform

# SynthPAI profile/review keys → our attribute codes (attributes-taxonomy.md).
_KEY_TO_CODE: dict[str, AttributeCode] = {
    "age": "age",
    "sex": "sex",
    "city_country": "location",
    "birth_city_country": "birthplace",
    "occupation": "occupation",
    "education": "education",
    "relationship_status": "relationship",
    "income_level": "income",
}


@dataclass(frozen=True)
class SynthPaiLabel:
    """One persona-level ground-truth label, with how revealed it is across the comments.

    `certainty` is the max over the persona's comment reviews (0 = no comment reveals the
    attribute — M2.2 may exclude it from the metric denominator); `hardness` is the min over
    the revealing comments (the easiest leaking comment sets the difficulty), None if never
    revealed.
    """

    value: str | int
    hardness: int | None
    certainty: int


@dataclass(frozen=True)
class SynthPaiPersona:
    """One synthetic persona: its authored comments + its 8 ground-truth labels."""

    author: str
    records: list[ParsedTextRecord]
    labels: dict[AttributeCode, SynthPaiLabel]


class SynthPaiPersonaAdapter:
    """`SourceAdapter` port over one persona's comments (platform=synthpai, method=loader)."""

    platform: Platform = "synthpai"
    method: Method = "loader"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


def _label_value(profile: Mapping[str, Any], key: str) -> str | int | None:
    value = profile.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _human_review(row: Mapping[str, Any], key: str) -> tuple[int, int] | None:
    """This comment's (hardness, certainty) for one attribute, or None if not reviewed."""
    reviews = row.get("reviews")
    if not isinstance(reviews, Mapping):
        return None
    human = reviews.get("human")
    if not isinstance(human, Mapping):
        return None
    entry = human.get(key)
    if not isinstance(entry, Mapping):
        return None
    hardness, certainty = entry.get("hardness"), entry.get("certainty")
    if not isinstance(hardness, int) or not isinstance(certainty, int):
        return None
    return hardness, certainty


def _labels_for(rows: list[Mapping[str, Any]]) -> dict[AttributeCode, SynthPaiLabel]:
    """The persona's labels from its (identical) per-row profile + aggregated reviews."""
    profile = rows[0].get("profile")
    if not isinstance(profile, Mapping):
        return {}
    labels: dict[AttributeCode, SynthPaiLabel] = {}
    for key, code in _KEY_TO_CODE.items():
        value = _label_value(profile, key)
        if value is None:
            continue
        hardness: int | None = None
        certainty = 0
        for row in rows:
            review = _human_review(row, key)
            if review is None:
                continue
            row_hardness, row_certainty = review
            certainty = max(certainty, row_certainty)
            # only comments that reveal the attribute, and only graded hardness — 0 is the
            # annotators' unset sentinel (the real scale is 1–5), even beside certainty > 0.
            if row_certainty > 0 and row_hardness > 0:
                hardness = row_hardness if hardness is None else min(hardness, row_hardness)
        labels[code] = SynthPaiLabel(value=value, hardness=hardness, certainty=certainty)
    return labels


def parse_synthpai_rows(rows: Iterable[Mapping[str, Any]]) -> list[SynthPaiPersona]:
    """Group JSONL rows by author into personas (records + labels), in first-seen order.

    Rows without an author or non-empty text are skipped. Every comment is authored by its
    persona (`is_subject_authored=True`) — the pipeline still runs the drop gate.
    """
    by_author: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        author = row.get("author")
        text = row.get("text")
        if not isinstance(author, str) or not isinstance(text, str) or not text.strip():
            continue
        by_author.setdefault(author, []).append(row)
    return [
        SynthPaiPersona(
            author=author,
            records=[
                ParsedTextRecord(text=str(row["text"]), is_subject_authored=True)
                for row in author_rows
            ],
            labels=_labels_for(author_rows),
        )
        for author, author_rows in by_author.items()
    ]
