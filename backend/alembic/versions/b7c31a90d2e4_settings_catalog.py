"""settings_catalog

Revision ID: b7c31a90d2e4
Revises: 05c4858bfab5
Create Date: 2026-08-04

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). This migration imports application code
(app.config.catalog) to seed the table - normally a replay hazard, here
neutralized because seed_catalog() is an upsert-plus-prune that converges on
the CURRENT constant: a from-scratch replay and an in-place upgrade land on
identical rows even after later constant edits (DECISIONS D47).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.config.catalog import seed_catalog

revision: str = "b7c31a90d2e4"
down_revision: str | None = "05c4858bfab5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings_catalog",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("enum_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("default_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("lowest_level", sa.String(length=16), nullable=False),
        sa.Column("secret", sa.Boolean(), nullable=False),
        sa.Column("resolution", sa.String(length=16), nullable=False),
        sa.Column("write_restricted", sa.String(length=30), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "value_type IN ('int','float','bool','string','object')",
            name=op.f("ck_settings_catalog_value_type_vocab"),
        ),
        sa.CheckConstraint(
            "lowest_level IN ('listener','aggregator','pod','deployment','organization','any')",
            name=op.f("ck_settings_catalog_lowest_level_vocab"),
        ),
        sa.CheckConstraint(
            "resolution IN ('override','inventory')",
            name=op.f("ck_settings_catalog_resolution_vocab"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings_catalog")),
    )
    seed_catalog(op.get_bind())


def downgrade() -> None:
    op.drop_table("settings_catalog")
