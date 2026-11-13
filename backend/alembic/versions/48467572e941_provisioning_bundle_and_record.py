"""provisioning_bundle and device_provisioning_record

Revision ID: 48467572e941
Revises: d5f28c60a419
Create Date: 2026-11-13

Task E4.1 (spec 8.5, 8.3; phase-4 §4; DECISIONS D166). Two tables: one row per
export, and one per device-to-card assignment inside it.

**Evidence, so almost nothing is a foreign key.** A bundle describes cards that
may already be in the field, and a record describes a device that may since
have been deleted, or may never have existed in inventory at all. Following the
D33/D55 precedent, `deployment_id`, `scope_id`, `pod_id`, `aggregator_uuid`,
`target_id` and `config_revision_id` are bare columns. The two real foreign
keys are `generated_by` (SET NULL, the `config_revision.created_by` convention)
and `bundle_id` (CASCADE). Bundles are never deleted by the platform, so the
cascade only keeps a development downgrade or a test teardown tidy.

The record status CHECK is spec 8.5's vocabulary rendered from
`models.RECORD_STATUSES`, so the constraint and the constant cannot disagree.

Reversible: `downgrade` drops both tables, record first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "48467572e941"
down_revision: str | None = "d5f28c60a419"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provisioning_bundle",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("firmware_key_version", sa.Integer(), nullable=False),
        sa.Column("archive_checksum", sa.String(length=80), nullable=False),
        sa.Column("generated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["generated_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "scope_type IN ('pod', 'deployment')",
            name="scope_type_vocab",
        ),
    )
    op.create_index(
        op.f("ix_provisioning_bundle_deployment_id"),
        "provisioning_bundle",
        ["deployment_id"],
    )

    op.create_table(
        "device_provisioning_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("pod_id", sa.Uuid(), nullable=False),
        sa.Column("aggregator_uuid", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("file_kind", sa.String(length=20), nullable=False),
        sa.Column("file_path", sa.String(length=300), nullable=False),
        sa.Column("config_revision_id", sa.Uuid(), nullable=True),
        sa.Column("config_checksum", sa.String(length=80), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'generated'"),
            nullable=False,
        ),
        sa.Column(
            "status_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["bundle_id"], ["provisioning_bundle.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bundle_id", "target_type", "target_id"),
        sa.CheckConstraint(
            "target_type IN ('aggregator','listener')",
            name="target_type_vocab",
        ),
        sa.CheckConstraint(
            "status IN ('generated', 'flashed', 'registered', 'confirmed', 'mismatch')",
            name="status_vocab",
        ),
        sa.CheckConstraint(
            "file_kind IN ('pod_file', 'listener_file', 'settings_yaml')",
            name="file_kind_vocab",
        ),
    )
    op.create_index(
        op.f("ix_device_provisioning_record_bundle_id"),
        "device_provisioning_record",
        ["bundle_id"],
    )
    op.create_index(
        op.f("ix_device_provisioning_record_deployment_id"),
        "device_provisioning_record",
        ["deployment_id"],
    )
    op.create_index(
        op.f("ix_device_provisioning_record_status"),
        "device_provisioning_record",
        ["status"],
    )
    op.create_index(
        "ix_device_provisioning_record_target",
        "device_provisioning_record",
        ["target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_device_provisioning_record_target", table_name="device_provisioning_record")
    op.drop_index(
        op.f("ix_device_provisioning_record_status"), table_name="device_provisioning_record"
    )
    op.drop_index(
        op.f("ix_device_provisioning_record_deployment_id"),
        table_name="device_provisioning_record",
    )
    op.drop_index(
        op.f("ix_device_provisioning_record_bundle_id"), table_name="device_provisioning_record"
    )
    op.drop_table("device_provisioning_record")
    op.drop_index(op.f("ix_provisioning_bundle_deployment_id"), table_name="provisioning_bundle")
    op.drop_table("provisioning_bundle")
