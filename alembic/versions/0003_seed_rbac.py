"""0003 seed RBAC — permissions + role_permissions from the rbac matrix.

Reference/seed data (migrations.md): the role→permission matrix mirrored into the DB from
``app.auth.rbac`` (the single source of truth), so the API check and the DB never drift.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op
from app.auth.rbac import PERMISSIONS, ROLE_PERMISSIONS

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for code, description in PERMISSIONS:
        op.execute(
            text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description)"
            ).bindparams(code=code, description=description)
        )
    for role, codes in ROLE_PERMISSIONS.items():
        for code in codes:
            op.execute(
                text(
                    "INSERT INTO role_permissions (role, permission_id) "
                    "SELECT CAST(:role AS role_t), id FROM permissions WHERE code = :code"
                ).bindparams(role=role, code=code)
            )


def downgrade() -> None:
    op.execute("DELETE FROM role_permissions")
    op.execute("DELETE FROM permissions")
