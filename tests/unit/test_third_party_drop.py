"""Unit (M1.2): the third-party drop gate — is_subject_authored=false never survives ingestion.

Mandatory security gate (rule 5 / third-party-drop.md): content the subject did not author is
discarded BEFORE encrypt/embed/persist, so it never reaches storage or the embedding index.
"""

from collections.abc import Iterable

from app.ingestion.base import Method, ParsedTextRecord, Platform
from app.ingestion.canonical import CanonicalTextItem
from app.services.ingestion import drop_third_party, run_ingestion


def _item(authored: bool, text: str = "some content") -> CanonicalTextItem:
    return CanonicalTextItem(
        text=text,
        posted_at=None,
        original_tz=None,
        platform="reddit",
        lang="en",
        is_subject_authored=authored,
    )


class _FakeAdapter:
    platform: Platform = "reddit"
    method: Method = "upload"

    def __init__(self, records: list[ParsedTextRecord]) -> None:
        self._records = records

    def parse(self) -> Iterable[ParsedTextRecord]:
        return self._records


def test_drop_keeps_only_subject_authored() -> None:
    kept = drop_third_party([_item(True, "mine"), _item(False, "theirs"), _item(True, "also mine")])
    assert [i.text for i in kept] == ["mine", "also mine"]


def test_drop_discards_all_third_party() -> None:
    assert drop_third_party([_item(False), _item(False)]) == []


def test_drop_is_a_noop_when_all_authored() -> None:
    items = [_item(True), _item(True)]
    assert drop_third_party(items) == items


def test_run_ingestion_drops_third_party_end_to_end() -> None:
    records = [
        ParsedTextRecord(
            text="My own English sentence about my weekend.", is_subject_authored=True
        ),
        ParsedTextRecord(
            text="A third party wrote this English sentence.", is_subject_authored=False
        ),
    ]
    items = run_ingestion(_FakeAdapter(records))
    # The rule-5 gate: the third-party item never survives to encrypt/embed/persist.
    assert len(items) == 1
    assert all(i.is_subject_authored for i in items)
