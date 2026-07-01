"""0001 init — the full v2 schema.

Tables, enums, and indexes are created from ``app.db.models`` (the source of truth), plus the
pgvector/pgcrypto extensions, the ``SECURITY DEFINER`` crypto functions, and the non-superuser
application role. RLS policies and the owned-table grants land in **0002 (M0.5)** together with
the isolation tests, so security-critical policies are never shipped untested — until then the
app role has **no access to owned tables** (fail-closed).

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op
from app.db.models import Base

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Indexes (03-data/database/indexes.md): FK + RLS, dedupe, time-purge, join, calibration lookup,
# and the pgvector HNSW index. UK/PK indexes come from the models (unique=True / primary_key).
_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX idx_items_owner_user_id ON items (owner_user_id)",
    "CREATE INDEX idx_items_content_hmac ON items (content_hmac)",
    "CREATE INDEX idx_items_expires_at ON items (expires_at) WHERE expires_at IS NOT NULL",
    "CREATE INDEX idx_media_assets_owner_user_id ON media_assets (owner_user_id)",
    "CREATE INDEX idx_media_assets_content_hmac ON media_assets (content_hmac)",
    "CREATE INDEX idx_media_assets_expires_at ON media_assets (expires_at) "
    "WHERE expires_at IS NOT NULL",
    "CREATE INDEX idx_exif_findings_media_asset_id ON exif_findings (media_asset_id)",
    "CREATE INDEX idx_connected_accounts_user_id ON connected_accounts (user_id)",
    "CREATE INDEX idx_consents_user_id ON consents (user_id)",
    "CREATE INDEX idx_memberships_user_id ON memberships (user_id)",
    "CREATE INDEX idx_memberships_org_id ON memberships (org_id)",
    "CREATE INDEX idx_profiles_user_id ON profiles (user_id)",
    "CREATE INDEX idx_import_sources_profile_id ON import_sources (profile_id)",
    "CREATE INDEX idx_runs_profile_id ON runs (profile_id)",
    "CREATE INDEX idx_inferences_profile_id ON inferences (profile_id)",
    "CREATE INDEX idx_inferences_run_id ON inferences (run_id)",
    "CREATE INDEX idx_inference_candidates_inference_id ON inference_candidates (inference_id)",
    "CREATE INDEX idx_inference_evidence_candidate_id ON inference_evidence (candidate_id)",
    "CREATE INDEX idx_run_metrics_run_id ON run_metrics (run_id)",
    "CREATE INDEX idx_eval_labels_profile_id ON eval_labels (profile_id)",
    "CREATE INDEX idx_eval_results_run_id ON eval_results (run_id)",
    "CREATE INDEX idx_remediations_profile_id ON remediations (profile_id)",
    "CREATE INDEX idx_remediations_inference_id ON remediations (inference_id)",
    "CREATE INDEX idx_calibration_lookup ON calibration "
    "(engine_version, attribute_code, modality, signal, n, confidence_bucket)",
    "CREATE INDEX idx_items_embedding_hnsw ON items USING hnsw (embedding vector_cosine_ops)",
)

# The SECURITY DEFINER crypto boundary (decrypt model A — the DEK never leaves Postgres).
# One statement per op.execute (asyncpg rejects multi-statement prepared queries).
_ENCRYPT_FN = """
CREATE OR REPLACE FUNCTION encrypt_field(p_user_id uuid, p_plaintext text, p_master_key text)
    RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $$
DECLARE v_dek text;
BEGIN
    SELECT pgp_sym_decrypt(wrapped_dek, p_master_key) INTO v_dek
    FROM data_keys WHERE user_id = p_user_id;
    IF v_dek IS NULL THEN
        RAISE EXCEPTION 'no data key for user %', p_user_id USING ERRCODE = 'no_data_found';
    END IF;
    RETURN pgp_sym_encrypt(p_plaintext, v_dek);
END;
$$
"""

_DECRYPT_FN = """
CREATE OR REPLACE FUNCTION decrypt_field(p_user_id uuid, p_ciphertext bytea, p_master_key text)
    RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $$
DECLARE v_dek text;
BEGIN
    SELECT pgp_sym_decrypt(wrapped_dek, p_master_key) INTO v_dek
    FROM data_keys WHERE user_id = p_user_id;
    IF v_dek IS NULL THEN
        RAISE EXCEPTION 'no data key for user %', p_user_id USING ERRCODE = 'no_data_found';
    END IF;
    RETURN pgp_sym_decrypt(p_ciphertext, v_dek);
END;
$$
"""

_PROVISION_FN = """
CREATE OR REPLACE FUNCTION provision_user_dek(p_user_id uuid, p_master_key text)
    RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public
AS $$
    INSERT INTO data_keys (user_id, wrapped_dek, kms_key_id)
    VALUES (p_user_id,
            pgp_sym_encrypt(encode(gen_random_bytes(32), 'hex'), p_master_key),
            'local-mvp');
$$
"""

# The non-superuser app role. Password is a LOCAL/TEST throwaway; prod provisions the role + a
# secret password out-of-band (Terraform, M7.2) and this IF NOT EXISTS skips. Owned-table grants
# + RLS land in 0002 — for now the role can only execute the crypto fns and read reference data.
_CREATE_APP_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'glasshouse_app') THEN
        CREATE ROLE glasshouse_app LOGIN PASSWORD 'glasshouse_app';
    END IF;
END
$$
"""

_GRANT_STATEMENTS: tuple[str, ...] = (
    "REVOKE ALL ON FUNCTION encrypt_field(uuid, text, text) FROM PUBLIC",
    "REVOKE ALL ON FUNCTION decrypt_field(uuid, bytea, text) FROM PUBLIC",
    "REVOKE ALL ON FUNCTION provision_user_dek(uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION encrypt_field(uuid, text, text) TO glasshouse_app",
    "GRANT EXECUTE ON FUNCTION decrypt_field(uuid, bytea, text) TO glasshouse_app",
    "GRANT SELECT ON attributes, calibration, permissions, role_permissions TO glasshouse_app",
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    Base.metadata.create_all(bind)
    for statement in _INDEX_STATEMENTS:
        op.execute(statement)
    op.execute(_ENCRYPT_FN)
    op.execute(_DECRYPT_FN)
    op.execute(_PROVISION_FN)
    op.execute(_CREATE_APP_ROLE)
    for statement in _GRANT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP FUNCTION IF EXISTS provision_user_dek(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS decrypt_field(uuid, bytea, text)")
    op.execute("DROP FUNCTION IF EXISTS encrypt_field(uuid, text, text)")
    Base.metadata.drop_all(bind)
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
    # The app role may own grants elsewhere; drop is left to ops (not auto-dropped here).
