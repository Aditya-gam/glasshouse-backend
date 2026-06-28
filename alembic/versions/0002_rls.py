"""0002 RLS — row-level security on the owned tables + app-role grants.

The fail-closed half of the schema (split from 0001 so these security-critical, join-based
policies ship with their isolation tests). Three scoping patterns (rls-policies.md):
  - direct      — `owner column = app_user_id()`           (items, media_assets, …)
  - profile     — `profile_id IN (app_owned_profile_ids())` (runs, inferences, …)
  - child       — `EXISTS (parent row)` → inherits the parent's RLS (run_metrics, evidence, …)
`data_keys` stays grant-locked (reachable only via the SECURITY DEFINER crypto fns).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# app_user_id(): the request's user from the GUC; '' (unset placeholder) → NULL → fail-closed.
# app_owned_profile_ids(): the user's profile ids, SECURITY DEFINER so it bypasses profiles' RLS
# (no policy recursion) — it only ever returns rows the GUC's user owns.
_HELPERS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_owned_profile_ids() RETURNS SETOF uuid
        LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, public
    AS $$ SELECT id FROM profiles WHERE user_id = app_user_id() $$
    """,
    "GRANT EXECUTE ON FUNCTION app_user_id() TO glasshouse_app",
    "GRANT EXECUTE ON FUNCTION app_owned_profile_ids() TO glasshouse_app",
)

# table -> the boolean scope expression used in both USING and WITH CHECK.
_DIRECT = {
    "profiles": "user_id = app_user_id()",
    "items": "owner_user_id = app_user_id()",
    "media_assets": "owner_user_id = app_user_id()",
    "connected_accounts": "user_id = app_user_id()",
    "consents": "user_id = app_user_id()",
}
_PROFILE_SCOPED = {
    "import_sources": "profile_id IN (SELECT app_owned_profile_ids())",
    "runs": "profile_id IN (SELECT app_owned_profile_ids())",
    "inferences": "profile_id IN (SELECT app_owned_profile_ids())",
    "remediations": "profile_id IN (SELECT app_owned_profile_ids())",
}
# child -> EXISTS against the parent (the parent's own RLS scopes the subquery).
_CHILD = {
    "run_metrics": "EXISTS (SELECT 1 FROM runs WHERE runs.id = run_metrics.run_id)",
    "inference_candidates": (
        "EXISTS (SELECT 1 FROM inferences WHERE inferences.id = inference_candidates.inference_id)"
    ),
    "inference_evidence": (
        "EXISTS (SELECT 1 FROM inference_candidates "
        "WHERE inference_candidates.id = inference_evidence.candidate_id)"
    ),
    "exif_findings": (
        "EXISTS (SELECT 1 FROM media_assets WHERE media_assets.id = exif_findings.media_asset_id)"
    ),
}
_OWNED = {**_DIRECT, **_PROFILE_SCOPED, **_CHILD}


def _secure(table: str, expr: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {table}_owner_scope ON {table} USING ({expr}) WITH CHECK ({expr})",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO glasshouse_app",
    )


def upgrade() -> None:
    for statement in _HELPERS:
        op.execute(statement)
    for table, expr in _OWNED.items():
        for statement in _secure(table, expr):
            op.execute(statement)


def downgrade() -> None:
    for table in _OWNED:
        op.execute(f"REVOKE ALL ON {table} FROM glasshouse_app")
        op.execute(f"DROP POLICY IF EXISTS {table}_owner_scope ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_owned_profile_ids()")
    op.execute("DROP FUNCTION IF EXISTS app_user_id()")
