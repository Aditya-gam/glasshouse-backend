"""0010 — public eval reads: app-role SELECT on eval_results + a latest-eval helper.

The trust view (`06-api/endpoints/eval.md`) serves two **public** (no-auth) numbers from Job 1:
`GET /v1/eval/results` and `/v1/eval/calibration`. `calibration` is already app-readable (0001);
`eval_results` is not — grant it here. Finding "the latest eval run" needs `runs`, which is
RLS-scoped to a profile owner, so a public app-role reader (no user, no RLS context) can't see it.
`latest_eval_run_id()` is a SECURITY DEFINER helper (same idiom as `app_owned_profile_ids()`, 0002)
that returns **only** the newest eval run's id — tighter than opening `runs` RLS to the app role.

Both statements are idempotent (re-GRANT is a no-op, CREATE OR REPLACE replaces in place), so no
guards are needed; fresh databases pick them up here (grants/functions live only in migrations).

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SECURITY DEFINER so they bypass `runs` RLS (no policy recursion); STABLE + a pinned search_path
# (the SECURITY DEFINER hardening from 0002). Both resolve the newest eval run for a public reader:
# its id (for eval_results) and its *bare* engine_version — the value the calibration map is keyed
# on, and the engine that produced this run's accuracy rows, so the trust view's two halves agree.
_LATEST_EVAL_ID_FN = """
CREATE OR REPLACE FUNCTION latest_eval_run_id() RETURNS uuid
    LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, public
AS $$ SELECT id FROM runs WHERE type = 'eval' ORDER BY created_at DESC LIMIT 1 $$
"""
_LATEST_EVAL_ENGINE_FN = """
CREATE OR REPLACE FUNCTION latest_eval_engine_version() RETURNS text
    LANGUAGE sql SECURITY DEFINER STABLE SET search_path = pg_catalog, public
AS $$ SELECT engine_version FROM runs WHERE type = 'eval' ORDER BY created_at DESC LIMIT 1 $$
"""


def upgrade() -> None:
    op.execute("GRANT SELECT ON eval_results TO glasshouse_app")
    op.execute(_LATEST_EVAL_ID_FN)
    op.execute(_LATEST_EVAL_ENGINE_FN)
    op.execute("GRANT EXECUTE ON FUNCTION latest_eval_run_id() TO glasshouse_app")
    op.execute("GRANT EXECUTE ON FUNCTION latest_eval_engine_version() TO glasshouse_app")


def downgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION latest_eval_engine_version() FROM glasshouse_app")
    op.execute("REVOKE EXECUTE ON FUNCTION latest_eval_run_id() FROM glasshouse_app")
    op.execute("DROP FUNCTION IF EXISTS latest_eval_engine_version()")
    op.execute("DROP FUNCTION IF EXISTS latest_eval_run_id()")
    op.execute("REVOKE SELECT ON eval_results FROM glasshouse_app")
