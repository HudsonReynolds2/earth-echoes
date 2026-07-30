"""role_assignments

Revision ID: 658a7e1ad594
Revises: c07e17281417
Create Date: 2026-07-24 15:13:49.688632

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). Autogenerate output is a draft; review it
by hand before committing.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401

from alembic import op  # noqa: F401

revision: str = "658a7e1ad594"
down_revision: str | None = "c07e17281417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "role_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name=op.f("fk_role_assignment_user_id_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignment")),
        sa.UniqueConstraint(
            "user_id", "role", "deployment_id", name=op.f("uq_role_assignment_user_id")
        ),
    )
    op.create_index(
        op.f("ix_role_assignment_user_id"), "role_assignment", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_role_assignment_user_id"), table_name="role_assignment")
    op.drop_table("role_assignment")
