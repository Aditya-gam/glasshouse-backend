"""Utility preservation (defend/utility-preservation.md, M3.5) — the floor vs cheat-by-delete.

A rewrite is only valid if it breaks the inference AND keeps the meaning. Without this, the
trivially "best" edit is to delete everything (confidence → 0, but the post is destroyed). Utility
is measured in two stages:
  1. a **free embedding pre-filter** (cosine of original vs edited) — drops obviously-destructive
     edits before spending a model call;
  2. an **independent LLM judge** (a slot separate from the anonymizer — editor ≠ judge), graded
     reference-anchored on **meaning preservation ignoring the sensitive attribute** plus a separate
     **readability** call (one criterion per call, to avoid halo).

Utility is scored against the **non-sensitive** intent, not the whole original — the edit *must*
change the sensitive part, so whole-original similarity would wrongly penalize the removal we want.
The floor is a **pre-filter, not a hard gate** (advise-only): below it we don't suggest an edit, but
the user may still choose a lower-utility option for maximum privacy. Content never logged.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.gateway.client import UtilityCriterion, UtilityGrade, UtilityVerdict
from app.retrieval.embedder import Embedder

# meaning grade → a 0..1 utility score (the headline; readability is a sub-score).
_GRADE_SCORE: dict[UtilityGrade, float] = {
    "fully": 1.0,
    "mostly": 0.75,
    "partially": 0.5,
    "lost": 0.0,
}
# An edit that keeps < this fraction of the original's length is gutted → obviously destructive.
# This is the reliable, embedder-independent destructive gate: the bge embedder L2-normalizes and
# scores even an empty edit ~0.55 cosine, so a cosine threshold alone cannot catch delete-by-cheat.
_MIN_LENGTH_RATIO = 0.25
# Cosine below this is a *secondary* screen for a semantically-unrelated replacement (bge's floor is
# high, so this is soft; the length guard is the hard gate). Tuning param (utility-preservation §7).
_EMBED_PREFILTER = 0.5
_DEFAULT_FLOOR = 0.5  # utility_score at/above this passes the pre-filter (utility-preservation §3)


class UtilityJudge(Protocol):
    """Grade one utility criterion of an edit (the gateway `judge` slot; test fakes conform)."""

    async def judge_utility(
        self, *, original: str, edited: str, attribute: str, criterion: UtilityCriterion
    ) -> UtilityVerdict: ...


@dataclass(frozen=True)
class UtilityAssessment:
    """How much of the non-sensitive meaning an edit preserved, + whether it clears the floor."""

    utility_score: float  # from the meaning grade (0..1)
    meaning: UtilityGrade
    readability: UtilityGrade | None  # None when the pre-filter rejected the edit (judge skipped)
    embedding_similarity: float
    passes_floor: bool


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors (0 when either is degenerate)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


async def assess_utility(
    judge: UtilityJudge,
    embedder: Embedder,
    *,
    original: str,
    edited: str,
    sensitive_attribute: str,
    floor: float = _DEFAULT_FLOOR,
) -> UtilityAssessment:
    """Score how much non-sensitive meaning `edited` preserves vs `original` (embedding then judge).

    The embedding pre-filter rejects obviously-destructive edits for free; surviving edits get the
    meaning + readability judge. Utility is the meaning grade; the floor is a suggestion threshold,
    not a hard block.
    """
    vectors = embedder.embed([original, edited])
    similarity = _cosine(vectors[0], vectors[1])
    stripped = edited.strip()
    gutted = not stripped or len(stripped) < _MIN_LENGTH_RATIO * len(original.strip())
    if gutted or similarity < _EMBED_PREFILTER:
        # obviously destructive (deleted/gutted, or semantically unrelated) — don't spend the judge;
        # utility is lost. The length guard catches the delete-by-cheat the cosine floor misses.
        return UtilityAssessment(
            utility_score=0.0,
            meaning="lost",
            readability=None,
            embedding_similarity=similarity,
            passes_floor=False,
        )
    meaning = await judge.judge_utility(
        original=original, edited=edited, attribute=sensitive_attribute, criterion="meaning"
    )
    readability = await judge.judge_utility(
        original=original, edited=edited, attribute=sensitive_attribute, criterion="readability"
    )
    utility_score = _GRADE_SCORE[meaning.grade]
    return UtilityAssessment(
        utility_score=utility_score,
        meaning=meaning.grade,
        readability=readability.grade,
        embedding_similarity=similarity,
        passes_floor=utility_score >= floor,
    )
