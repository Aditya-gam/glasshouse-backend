"""The Retriever — high-recall evidence selection for the attack (text-inference.md §3).

Always-on, Tier-1 (no LLM): the union of three signals — embedding-relevance (one pgvector query
per attribute), recency, and Presidio-flagged always-include — deduped and capped by a per-run
token budget. Recall-first: a footprint smaller than the budget passes through whole, and
always-include items are never ranked out. Returns one global evidence set for the joint Profiler
pass (M1.7). No content is logged.

Parameters (k / N / budget) are measure-then-fix — tuned on SynthPAI at M2; changing them (or the
attribute queries) changes the engine → recompute benchmarking + calibration (hard-invalidation).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.repositories import items as items_repo
from app.repositories.items import RetrievedItem
from app.retrieval.embedder import Embedder
from app.retrieval.pii import PiiDetector
from app.retrieval.queries import ATTRIBUTE_QUERIES
from app.retrieval.tokens import TokenCounter, count_tokens


@dataclass(frozen=True)
class RetrievalConfig:
    """Recall-first sizing (text-inference.md §10) — measured then fixed on SynthPAI at M2."""

    per_attribute_k: int = 8
    recency_n: int = 20
    token_budget: int = 6000


def _dedupe(ids: list[UUID]) -> list[UUID]:
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item_id in ids:
        if item_id not in seen:
            seen.add(item_id)
            ordered.append(item_id)
    return ordered


async def retrieve_evidence(
    conn: AsyncConnection,
    embedder: Embedder,
    pii_detector: PiiDetector,
    *,
    profile_id: UUID,
    master_key: str,
    config: RetrievalConfig | None = None,
    token_counter: TokenCounter = count_tokens,
) -> list[RetrievedItem]:
    """Select one profile's evidence set, RLS-scoped (the Profiler reads exactly this).

    Scoped to the run's profile, not the whole owner — a user (or the benchmark user at M2) can
    hold several profiles, and evidence must never blend across them.
    """
    config = config or RetrievalConfig()
    all_items = await items_repo.list_items_with_text(conn, profile_id, master_key)
    text_by_id = {item.id: item.text for item in all_items}

    # 1. embedding-relevance — one query per attribute, top-k each (pgvector / HNSW).
    relevant: list[UUID] = []
    for query in ATTRIBUTE_QUERIES.values():
        query_vector = embedder.embed([query])[0]
        relevant.extend(
            await items_repo.search_item_ids_by_embedding(
                conn, profile_id, query_vector, config.per_attribute_k
            )
        )
    # 2. recency.
    recent = await items_repo.recent_item_ids(conn, profile_id, config.recency_n)
    # 3. always-include — explicit-PII items must never be ranked out.
    mandatory = {item.id for item in all_items if pii_detector.has_identifying_signal(item.text)}

    selected: list[UUID] = []
    used = 0
    for item_id in _dedupe(list(mandatory) + relevant + recent):
        item_text = text_by_id.get(item_id)
        if item_text is None:
            continue
        item_tokens = token_counter(item_text)
        if item_id in mandatory or used + item_tokens <= config.token_budget:
            selected.append(item_id)
            used += item_tokens
    return [RetrievedItem(id=item_id, text=text_by_id[item_id]) for item_id in selected]
