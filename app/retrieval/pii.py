"""Explicit-PII detection for the Retriever's always-include signal (text-inference.md §3).

Items carrying explicit location / identity tokens are high-value and must never be ranked out of
the evidence set. A DI port keeps the (heavy) detector swappable and lets tests inject a fake — the
real Presidio engine loads a spaCy model on construction (download once: `spacy download
en_core_web_lg`), so CI uses the fake and never constructs it.
"""

from functools import lru_cache
from typing import Protocol

from presidio_analyzer import AnalyzerEngine

# Default-supported Presidio entities that mark an item as identity-bearing (employer/ORG needs a
# configured spaCy recognizer — a follow-up; LOCATION + PERSON + NRP cover location + identity).
_IDENTIFYING_ENTITIES = ["LOCATION", "PERSON", "NRP"]


class PiiDetector(Protocol):
    """Does the text carry an explicit identifying signal (→ always-include)?"""

    def has_identifying_signal(self, text: str) -> bool: ...


class PresidioPiiDetector:
    """Microsoft Presidio (Apache-2.0) NER-based detection. Loads a spaCy model on construction."""

    def __init__(self) -> None:
        self._analyzer = AnalyzerEngine()

    def has_identifying_signal(self, text: str) -> bool:
        results = self._analyzer.analyze(text=text, language="en", entities=_IDENTIFYING_ENTITIES)
        return len(results) > 0


@lru_cache(maxsize=1)
def default_pii_detector() -> PresidioPiiDetector:
    """The process-wide detector (loads the model once; used by the retriever at M1.7+)."""
    return PresidioPiiDetector()
