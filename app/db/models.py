"""SQLAlchemy 2.0 models for the v2 schema (03-data/database/tables/* + er-diagram.md).

The authoritative ORM mapping: Alembic ``0001_init`` (M0.4) autogenerates the migration from
these (the T2 tracer schema was retired at M1.9b). Two native enums
(``role_t``, ``run_status_t``); every other controlled vocabulary is ``text`` + ``CHECK``
(migrations.md). Mapping only — SQL lives in repositories. RLS/encryption are applied by the
migration (M0.4); these models carry the structure (columns, types, FKs, constraints).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# fastembed bge-small-en-v1.5 (local, $0); items.embedding is invertible → personal data.
_EMBEDDING_DIM = 384

# The only two native enums; all other controlled vocabularies are text + CHECK.
role_t = SAEnum("owner", "admin", "analyst", "viewer", name="role_t")
run_status_t = SAEnum("queued", "running", "succeeded", "failed", "canceled", name="run_status_t")


class Base(DeclarativeBase):
    pass


class _UuidPk:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


class _Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ----------------------------------------------------------------- identity --
class User(_UuidPk, _Timestamped, Base):
    __tablename__ = "users"

    clerk_user_id: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str | None] = mapped_column(Text)


class Organization(_UuidPk, _Timestamped, Base):
    __tablename__ = "organizations"

    clerk_org_id: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)


class Membership(_UuidPk, _Timestamped, Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(role_t)


class Permission(_UuidPk, Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role: Mapped[str] = mapped_column(role_t, primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class DataKey(_Timestamped, Base):
    __tablename__ = "data_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    kms_key_id: Mapped[str] = mapped_column(Text)


class Consent(_UuidPk, Base):
    __tablename__ = "consents"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(Text)
    special_category: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    policy_version: Mapped[str] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- ingestion --
class Profile(_UuidPk, _Timestamped, Base):
    __tablename__ = "profiles"
    __table_args__ = (CheckConstraint("type IN ('self','synthpai')", name="ck_profiles_type"),)

    type: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )


class ConnectedAccount(_UuidPk, _Timestamped, Base):
    __tablename__ = "connected_accounts"
    __table_args__ = (
        CheckConstraint("platform IN ('reddit','mastodon','x')", name="ck_conn_platform"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    access_token_ct: Mapped[bytes] = mapped_column(LargeBinary)
    refresh_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    scopes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)


class ImportSource(_UuidPk, _Timestamped, Base):
    __tablename__ = "import_sources"
    __table_args__ = (
        CheckConstraint("method IN ('upload','connector','loader')", name="ck_import_method"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    connected_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("connected_accounts.id", ondelete="SET NULL")
    )


class Item(_UuidPk, _Timestamped, Base):
    __tablename__ = "items"
    # Keyed-HMAC dedupe is per-tenant: re-imports skip via ON CONFLICT on (owner_user_id, hmac).
    __table_args__ = (
        UniqueConstraint("owner_user_id", "content_hmac", name="uq_items_owner_content_hmac"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    import_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_sources.id", ondelete="SET NULL")
    )
    text_ct: Mapped[bytes] = mapped_column(LargeBinary)
    content_hmac: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(_EMBEDDING_DIM))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    original_tz: Mapped[str | None] = mapped_column(Text)
    is_subject_authored: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAsset(_UuidPk, _Timestamped, Base):
    __tablename__ = "media_assets"

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    import_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_sources.id", ondelete="SET NULL")
    )
    object_ref: Mapped[str] = mapped_column(Text)
    content_hmac: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    is_subject_authored: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExifFinding(_UuidPk, Base):
    __tablename__ = "exif_findings"
    __table_args__ = (
        CheckConstraint(
            "finding_type IN ('gps','timestamp','camera','software')", name="ck_exif_type"
        ),
    )

    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE")
    )
    finding_type: Mapped[str] = mapped_column(Text)
    value_ct: Mapped[bytes] = mapped_column(LargeBinary)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------ attack engine --
class Attribute(Base):
    __tablename__ = "attributes"
    __table_args__ = (
        CheckConstraint(
            "value_type IN ('numeric','categorical','geo_hier','freetext_semantic')",
            name="ck_attr_value_type",
        ),
    )

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(Text)
    match_method: Mapped[str] = mapped_column(Text)
    is_art9: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_sensitive_tier: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    allowed_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    severity: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Run(_UuidPk, _Timestamped, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("type IN ('attack','eval','remediation')", name="ck_runs_type"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(run_status_t)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Inference(_UuidPk, _Timestamped, Base):
    __tablename__ = "inferences"
    __table_args__ = (
        CheckConstraint("modality IN ('text','image','multimodal')", name="ck_inf_modality"),
        CheckConstraint("status IN ('inferred','abstained')", name="ck_inf_status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    attribute_code: Mapped[str] = mapped_column(ForeignKey("attributes.code"))
    modality: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    engine_version: Mapped[str] = mapped_column(Text)
    reasoning_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    reasoning_reveals_art9: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class InferenceCandidate(_UuidPk, Base):
    __tablename__ = "inference_candidates"

    inference_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inferences.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    value_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    raw_confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    confidence_source: Mapped[str | None] = mapped_column(Text)
    agreement: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    calibrated_reliability: Mapped[Decimal | None] = mapped_column(Numeric)


class InferenceEvidence(_UuidPk, Base):
    __tablename__ = "inference_evidence"
    __table_args__ = (
        CheckConstraint(
            "ref_type IN ('item','media_asset','exif_finding')", name="ck_evidence_ref_type"
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inference_candidates.id", ondelete="CASCADE")
    )
    ref_type: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    modality: Mapped[str] = mapped_column(Text)
    span: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    region: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proxy_score: Mapped[Decimal | None] = mapped_column(Numeric)
    citation_frequency: Mapped[Decimal | None] = mapped_column(Numeric)
    marginal_effect: Mapped[Decimal | None] = mapped_column(Numeric)
    rationale_ct: Mapped[bytes | None] = mapped_column(LargeBinary)


class RunMetric(_UuidPk, _Timestamped, Base):
    __tablename__ = "run_metrics"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    model_calls: Mapped[int | None] = mapped_column(Integer)


# --------------------------------------------------------------- measure --
class EvalLabel(_UuidPk, Base):
    __tablename__ = "eval_labels"
    # One label per (profile, attribute, modality) — the seed upserts on this (migration 0006).
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "attribute_code",
            "modality",
            name="uq_eval_labels_profile_attr_modality",
        ),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    attribute_code: Mapped[str] = mapped_column(ForeignKey("attributes.code"))
    true_value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    modality: Mapped[str] = mapped_column(Text)


class EvalResult(_UuidPk, Base):
    __tablename__ = "eval_results"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    attribute_code: Mapped[str] = mapped_column(ForeignKey("attributes.code"))
    modality: Mapped[str] = mapped_column(Text)
    top1_acc: Mapped[Decimal | None] = mapped_column(Numeric)
    top3_acc: Mapped[Decimal | None] = mapped_column(Numeric)
    by_hardness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    engine_version: Mapped[str] = mapped_column(Text)


class Calibration(_UuidPk, Base):
    __tablename__ = "calibration"

    engine_version: Mapped[str] = mapped_column(Text)
    attribute_code: Mapped[str] = mapped_column(ForeignKey("attributes.code"))
    modality: Mapped[str] = mapped_column(Text)
    signal: Mapped[str] = mapped_column(Text)
    n: Mapped[int] = mapped_column(Integer)
    confidence_bucket: Mapped[Decimal] = mapped_column(Numeric)
    empirical_accuracy: Mapped[Decimal] = mapped_column(Numeric)
    noise_std: Mapped[Decimal | None] = mapped_column(Numeric)
    ece: Mapped[Decimal | None] = mapped_column(Numeric)


# --------------------------------------------------------------- defend --
class Remediation(_UuidPk, _Timestamped, Base):
    __tablename__ = "remediations"
    __table_args__ = (
        CheckConstraint(
            "action IN ('rewrite','remove','strip_exif','crop','inpaint','decoy')",
            name="ck_remediation_action",
        ),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    inference_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inferences.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(Text)
    edited_text_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    span_changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    artifact_ref: Mapped[str | None] = mapped_column(Text)
    confidence_before: Mapped[Decimal | None] = mapped_column(Numeric)
    confidence_after: Mapped[Decimal | None] = mapped_column(Numeric)
    ci_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ci_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    significant: Mapped[bool | None] = mapped_column(Boolean)
    value_recovery_before: Mapped[bool | None] = mapped_column(Boolean)
    value_recovery_after: Mapped[bool | None] = mapped_column(Boolean)
    utility_score: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_decoy: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    evaluator_engine_version: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------- audit --
class AuditLog(_UuidPk, _Timestamped, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "action IN ('consent_granted','consent_revoked','export','erase',"
            "'connect','disconnect')",
            name="ck_audit_action",
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
