"""entity_override

Revision ID: c9d42be17a05
Revises: b7c31a90d2e4
Create Date: 2026-08-04

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9d42be17a05"
down_revision: str | None = "b7c31a90d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_override",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('organization','deployment','pod','aggregator','listener')",
            name=op.f("ck_entity_override_entity_type_vocab"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_override")),
        sa.UniqueConstraint(
            "entity_type", "entity_id", name=op.f("uq_entity_override_entity_type")
        ),
    )


def downgrade() -> None:
    op.drop_table("entity_override")
