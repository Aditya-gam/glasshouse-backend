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


async def get_calibrated_reliability(
    conn: AsyncConnection,
    *,
    engine_version: str,
    attribute_code: str,
    modality: str,
    signal: str,
    n: int,
    confidence_bucket: float,
) -> Decimal | None:
    """The calibrated reliability for one bucket, or None if the map has no matching row (M2.5).

    A miss is meaningful: an inference whose (engine_version, …, bucket) is absent from the map has
    no calibrated reliability, so per-user scoring fails closed (stores NULL) rather than surface a
    raw number. The `engine_version` must be the inference's own (the attack engine), which is what
    the map is keyed on (calibration.md: "must match the inference's").
    """
    result = await conn.execute(
        text(
            "SELECT empirical_accuracy FROM calibration "
            "WHERE engine_version = :ev AND attribute_code = :attr AND modality = :modality "
            "AND signal = :signal AND n = :n AND confidence_bucket = :bucket"
        ),
        {
            "ev": engine_version,
            "attr": attribute_code,
            "modality": modality,
            "signal": signal,
            "n": n,
            "bucket": Decimal(str(confidence_bucket)),
        },
    )
    row = result.first()
    if row is None:
        return None
    reliability: Decimal = row[0]
    return reliability


async def list_calibration_points(
    conn: AsyncConnection, engine_version: str
) -> list[tuple[float, float]]:
    """The reliability curve for an engine — `[(predicted, empirical)]` per confidence bucket (M2).

    The public trust display's reliability curve. The map holds a row per (attribute, signal, n)
    bucket; this pools them into one curve per predicted bucket by an **equal-weight macro-average**
    of `empirical_accuracy`. NOTE (known limitation): that is not the sample-weighted frequency the
    display implies — a low-sample cell sways a bucket as much as a high-sample one — but the
    per-cell sample count needed to weight correctly is not persisted (`calibration.n` is the
    self-consistency N, part of the key). Sample-weighting is a follow-up on the eval write path.
    `engine_version` is the bare attack engine the map is keyed on (never a judge-suffixed scoring
    version). Empty until a benchmark runs. `calibration` has no RLS; app-role SELECT is from 0001.
    """
    result = await conn.execute(
        text(
            "SELECT confidence_bucket, avg(empirical_accuracy) FROM calibration "
            "WHERE engine_version = :ev GROUP BY confidence_bucket ORDER BY confidence_bucket"
        ),
        {"ev": engine_version},
    )
    return [(float(row[0]), float(row[1])) for row in result]


async def get_noise_std(
    conn: AsyncConnection, *, engine_version: str, attribute_code: str, modality: str = "text"
) -> Decimal | None:
    """The adversary's run-to-run noise std for an attribute (the remediation noise floor, M3.7).

    Averaged across confidence buckets for the (engine, attribute, modality); NULL until the Job-1
    noise model is estimated — the caller then falls back to a provisional margin. Non-null only if
    at least one bucket has a measured `noise_std`.
    """
    result = await conn.execute(
        text(
            "SELECT avg(noise_std) FROM calibration "
            "WHERE engine_version = :ev AND attribute_code = :attr AND modality = :modality "
            "AND noise_std IS NOT NULL"
        ),
        {"ev": engine_version, "attr": attribute_code, "modality": modality},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    noise_std: Decimal = row[0]
    return noise_std
