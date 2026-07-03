"""Data access for `calibration` — the engine's reliability map (Job 1).

Written on a **privileged** connection by the eval service (M2.4); the app role has SELECT (M2.5
per-user scoring looks the map up). One row per (engine_version, attribute, modality, signal, n,
confidence_bucket); re-benchmarking the same engine upserts in place. No data subject → no crypto.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def upsert_calibration_bucket(
    conn: AsyncConnection,
    *,
    engine_version: str,
    attribute_code: str,
    modality: str,
    signal: str,
    n: int,
    confidence_bucket: float,
    empirical_accuracy: float,
    ece: float,
    noise_std: float | None = None,
) -> None:
    """Insert or refresh one calibration bucket for an engine version (the map's lookup key)."""
    await conn.execute(
        text(
            "INSERT INTO calibration (engine_version, attribute_code, modality, signal, n, "
            "confidence_bucket, empirical_accuracy, noise_std, ece) "
            "VALUES (:ev, :attr, :modality, :signal, :n, :bucket, :accuracy, :noise, :ece) "
            "ON CONFLICT (engine_version, attribute_code, modality, signal, n, confidence_bucket) "
            "DO UPDATE SET empirical_accuracy = EXCLUDED.empirical_accuracy, "
            "noise_std = EXCLUDED.noise_std, ece = EXCLUDED.ece"
        ),
        {
            "ev": engine_version,
            "attr": attribute_code,
            "modality": modality,
            "signal": signal,
            "n": n,
            "bucket": Decimal(str(confidence_bucket)),
            "accuracy": Decimal(str(empirical_accuracy)),
            "noise": None if noise_std is None else Decimal(str(noise_std)),
            "ece": Decimal(str(ece)),
        },
    )
