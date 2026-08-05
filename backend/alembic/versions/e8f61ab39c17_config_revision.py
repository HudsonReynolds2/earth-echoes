"""config_revision

Revision ID: e8f61ab39c17
Revises: d1e53fa27b06
Create Date: 2026-08-04

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). target_id and deployment_id are
deliberately NOT foreign keys (D55, the D33 precedent): revision history is
immutable evidence that outlives devices and deployments.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8f61ab39c17"
down_revision: str | None = "d1e53fa27b06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_type IN ('aggregator','listener')",
            name=op.f("ck_config_revision_target_type_vocab"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
            name=op.f("fk_config_revision_created_by_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_revision")),
    )
    op.create_index(
        op.f("ix_config_revision_deployment_id"), "config_revision", ["deployment_id"], unique=False
    )
    op.create_index(op.f("ix_config_revision_state"), "config_revision", ["state"], unique=False)
    op.create_index(
        "ix_config_revision_target",
        "config_revision",
        ["target_type", "target_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_config_revision_target", table_name="config_revision")
    op.drop_index(op.f("ix_config_revision_state"), table_name="config_revision")
    op.drop_index(op.f("ix_config_revision_deployment_id"), table_name="config_revision")
    op.drop_table("config_revision")
