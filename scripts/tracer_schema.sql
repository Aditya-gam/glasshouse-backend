-- T2 tracer-bullet schema: the thinnest slice that proves the security foundation
-- (envelope crypto + RLS isolation + the SECURITY DEFINER decrypt boundary).
--
-- SUPERSEDED by Alembic 0001_init at M0.4, which lifts this DDL (functions, RLS,
-- the app role, grants) into the full v2 schema. Until then this file is applied
-- to a fresh database by the test harness, as the owning superuser.
--
-- Decrypt model A (decision, T2): the per-user DEK never leaves Postgres. The
-- SECURITY DEFINER functions unwrap it inside the database; the app passes only
-- the env MASTER_KEY, as a bound parameter (prod swaps the master key for KMS).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Non-superuser application role: RLS-enforced, NO access to data_keys, EXECUTE on
-- the crypto functions only. The app connects as this role; superuser/owner bypass RLS.
-- The password here is a LOCAL/TEST throwaway (like the compose creds). When M0.4 lifts
-- this into Alembic, the role's password MUST come from env/secret — never committed.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'glasshouse_app') THEN
    CREATE ROLE glasshouse_app LOGIN PASSWORD 'glasshouse_app';
  END IF;
END
$$;

-- ---------------------------------------------------------------- tables --
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clerk_user_id text UNIQUE,
    email         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- The KMS-wrapped per-user DEK. The crypto-shred target; no app-readable grant.
CREATE TABLE IF NOT EXISTS data_keys (
    user_id     uuid PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    wrapped_dek bytea NOT NULL,
    kms_key_id  text  NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    text_ct             bytea NOT NULL,          -- T2 pgcrypto ciphertext
    content_hmac        text  NOT NULL,          -- keyed HMAC for dedupe (not a plain hash)
    is_subject_authored boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_owner_user_id ON items (owner_user_id);

-- The async unit of work. Tracer slice: owner_user_id is denormalized (M0.3 reworks RLS
-- to go through profile_id), status is text (M0.4 adds the run_status_t enum), and the run
-- executes synchronously (M1.9 moves it onto the arq queue) — the API shape is unchanged.
CREATE TABLE IF NOT EXISTS runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    type            text NOT NULL,   -- attack | eval | remediation
    status          text NOT NULL,   -- queued | running | succeeded | failed | canceled
    engine_version  text,
    idempotency_key text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    -- Idempotency is per-tenant: two users may pick the same client-generated key. NULL keys
    -- (runs without one) don't collide. (v2 model's global unique → revisit to this at M1.9.)
    UNIQUE (owner_user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_owner_user_id ON runs (owner_user_id);

-- Minimal inferences parent for the tracer bullet. owner_user_id is denormalized here;
-- M0.3 reworks RLS to go through profile_id and profile_id becomes an FK (M0.3). top_value_text
-- holds the leading candidate's value inline (non-sensitive here); M1.7 normalizes the ranked
-- candidates/evidence into their own tables.
CREATE TABLE IF NOT EXISTS inferences (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id  uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    run_id         uuid REFERENCES runs (id) ON DELETE CASCADE,
    profile_id     uuid,
    attribute_code text NOT NULL,
    modality       text NOT NULL DEFAULT 'text',
    status         text NOT NULL DEFAULT 'inferred',
    top_value_text text,
    reasoning_ct   bytea,                          -- T2 pgcrypto ciphertext (Art. 9-scrubbed)
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inferences_owner_user_id ON inferences (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_inferences_run_id ON inferences (run_id);

-- ------------------------------------------------------------------- RLS --
-- FORCE so the table owner is subject too. An unscoped session must match no rows:
-- once a custom GUC placeholder exists in a session, current_setting(..., true) returns
-- '' (not NULL), so NULLIF(..., '')::uuid yields NULL → owner_user_id = NULL → no rows
-- (fail-closed, and no "invalid uuid" error on the empty string).
ALTER TABLE items ENABLE ROW LEVEL SECURITY;
ALTER TABLE items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS items_owner_scope ON items;
CREATE POLICY items_owner_scope ON items
    USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

ALTER TABLE inferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE inferences FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inferences_owner_scope ON inferences;
CREATE POLICY inferences_owner_scope ON inferences
    USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS runs_owner_scope ON runs;
CREATE POLICY runs_owner_scope ON runs
    USING (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    WITH CHECK (owner_user_id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ------------------------------------------------------ crypto functions --
-- SECURITY DEFINER + a fixed search_path (no search_path injection). The master key
-- arrives as a bound parameter; the DEK is unwrapped into a local variable and never
-- string-interpolated, so neither key reaches the query log.
CREATE OR REPLACE FUNCTION encrypt_field(p_user_id uuid, p_plaintext text, p_master_key text)
    RETURNS bytea
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
AS $$
DECLARE
    v_dek text;
BEGIN
    SELECT pgp_sym_decrypt(wrapped_dek, p_master_key) INTO v_dek
    FROM data_keys WHERE user_id = p_user_id;
    IF v_dek IS NULL THEN
        RAISE EXCEPTION 'no data key for user %', p_user_id USING ERRCODE = 'no_data_found';
    END IF;
    RETURN pgp_sym_encrypt(p_plaintext, v_dek);
END;
$$;

CREATE OR REPLACE FUNCTION decrypt_field(p_user_id uuid, p_ciphertext bytea, p_master_key text)
    RETURNS text
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
AS $$
DECLARE
    v_dek text;
BEGIN
    SELECT pgp_sym_decrypt(wrapped_dek, p_master_key) INTO v_dek
    FROM data_keys WHERE user_id = p_user_id;
    IF v_dek IS NULL THEN
        RAISE EXCEPTION 'no data key for user %', p_user_id USING ERRCODE = 'no_data_found';
    END IF;
    RETURN pgp_sym_decrypt(p_ciphertext, v_dek);
END;
$$;

-- Provision a fresh per-user DEK, generated AND wrapped entirely in SQL (the raw DEK
-- never enters the application process). A privileged operation — owner only.
CREATE OR REPLACE FUNCTION provision_user_dek(p_user_id uuid, p_master_key text)
    RETURNS void
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
AS $$
    INSERT INTO data_keys (user_id, wrapped_dek, kms_key_id)
    VALUES (
        p_user_id,
        pgp_sym_encrypt(encode(gen_random_bytes(32), 'hex'), p_master_key),
        'local-mvp'
    );
$$;

-- -------------------------------------------------------------- privileges --
GRANT SELECT, INSERT, UPDATE, DELETE ON items, inferences, runs TO glasshouse_app;
-- Deliberately NO grant on data_keys → the app role cannot read key material.
REVOKE ALL ON FUNCTION encrypt_field(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION decrypt_field(uuid, bytea, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION provision_user_dek(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION encrypt_field(uuid, text, text) TO glasshouse_app;
GRANT EXECUTE ON FUNCTION decrypt_field(uuid, bytea, text) TO glasshouse_app;
-- provision_user_dek stays owner-only (no app grant): DEK creation is privileged.
