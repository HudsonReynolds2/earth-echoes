"""role_assignment_deployment_fk

Revision ID: 53181716569c
Revises: ee260dc1c1a8
Create Date: 2026-08-02 18:58:38.126712

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). Autogenerate output is a draft; review it
by hand before committing.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401

from alembic import op  # noqa: F401

revision: str = "53181716569c"
down_revision: str | None = "ee260dc1c1a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Scoped grants that reference no real deployment are meaningless once the
    # foreign key exists. They are DELETED, not NULLed: NULL means the grant is
    # organization-wide (E0.7), so NULLing an orphan would silently escalate a
    # scoped grant to org-wide (DECISIONS D33).
    op.execute(
        "DELETE FROM role_assignment WHERE deployment_id IS NOT NULL "
        "AND deployment_id NOT IN (SELECT id FROM deployment)"
    )
    op.create_foreign_key(
        op.f("fk_role_assignment_deployment_id_deployment"),
        "role_assignment",
        "deployment",
        ["deployment_id"],
        ["id"],
    )


def downgrade() -> None:
    # Drops the foreign key only. Grants deleted by upgrade() referenced
    # deployments that never existed; they are not restorable (D33 records
    # this as accepted data loss for rows that were unusable by definition).
    op.drop_constraint(
        op.f("fk_role_assignment_deployment_id_deployment"),
        "role_assignment",
        type_="foreignkey",
    )
