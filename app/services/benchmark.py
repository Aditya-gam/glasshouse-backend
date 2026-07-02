"""Benchmark seeding (M2.1) — SynthPAI personas → benchmark user + profiles + items + labels.

Runs on a **privileged** connection (an ops-time seed, like a migration): `eval_labels` has no
app-role grant, and the synthetic-data owner is provisioned here. Items flow through the normal
ingestion service (encrypt + embed + HMAC-dedupe), so the M2.2 eval run reads them through the
app role under RLS exactly like a real user's — one code path. Ids are uuid5-deterministic, so
re-seeding is idempotent. Deviation from loader-synthpai.md noted there: the shipped RLS/schema
has no NULL-owner plaintext path, so synthetic items are stored encrypted under a system user.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.crypto import has_user_dek, provision_user_dek
from app.ingestion.sources.synthpai import SynthPaiPersona, SynthPaiPersonaAdapter
from app.repositories import eval_labels as eval_labels_repo
from app.repositories import profiles as profiles_repo
from app.repositories import users as users_repo
from app.retrieval.embedder import Embedder
from app.services.ingestion import ingest_and_persist

_BENCHMARK_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "glasshouse:benchmark")
# The one system user that owns all synthetic benchmark data (never crypto-shredded).
SYNTHPAI_USER_ID = uuid.uuid5(_BENCHMARK_NAMESPACE, "synthpai-user")


def synthpai_profile_id(author: str) -> uuid.UUID:
    """The stable profile id for one SynthPAI persona (same author → same profile)."""
    return uuid.uuid5(_BENCHMARK_NAMESPACE, f"synthpai-profile:{author}")


def _synthpai_source_id(author: str) -> uuid.UUID:
    """The stable import-source id per persona — re-seeding reuses it (no provenance growth)."""
    return uuid.uuid5(_BENCHMARK_NAMESPACE, f"synthpai-source:{author}")


@dataclass(frozen=True)
class SeedResult:
    """Outcome of one seed pass (counts only — never content)."""

    personas: int
    items_inserted: int
    items_deduped: int
    labels_upserted: int


async def seed_synthpai(
    conn: AsyncConnection,
    embedder: Embedder,
    personas: list[SynthPaiPersona],
    *,
    master_key: str,
) -> SeedResult:
    """Seed the text benchmark: per persona, a profile + its items + its ground-truth labels."""
    await users_repo.ensure_user(conn, SYNTHPAI_USER_ID)
    if not await has_user_dek(conn, SYNTHPAI_USER_ID):
        await provision_user_dek(conn, SYNTHPAI_USER_ID, master_key)
    inserted = deduped = labels = 0
    for persona in personas:
        profile_id = synthpai_profile_id(persona.author)
        await profiles_repo.ensure_profile(
            conn, profile_id, profile_type="synthpai", user_id=SYNTHPAI_USER_ID
        )
        result = await ingest_and_persist(
            conn,
            embedder,
            SynthPaiPersonaAdapter(persona.records),
            owner_user_id=SYNTHPAI_USER_ID,
            master_key=master_key,
            profile_id=profile_id,
            import_source_id=_synthpai_source_id(persona.author),
        )
        inserted += result.inserted
        deduped += result.deduped
        for code, label in persona.labels.items():
            await eval_labels_repo.upsert_eval_label(
                conn,
                profile_id=profile_id,
                attribute_code=code,
                true_value={
                    "value": label.value,
                    "hardness": label.hardness,
                    "certainty": label.certainty,
                },
                modality="text",
            )
            labels += 1
    return SeedResult(
        personas=len(personas),
        items_inserted=inserted,
        items_deduped=deduped,
        labels_upserted=labels,
    )
