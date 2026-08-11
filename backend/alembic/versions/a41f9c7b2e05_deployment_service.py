"""deployment_service

Revision ID: a41f9c7b2e05
Revises: e8f61ab39c17
Create Date: 2026-08-10

Task E3.1 (spec 7.1). Broker connection storage: E3 populates the 'mqtt'
service_key only, and E5 extends this table with the remaining deployment
services, their connection tests, and the spec 16.5 status lifecycle.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). deployment_id IS a real foreign key here -
unlike the immutable-evidence tables (D33/D55), a service row describes a
live connection and has no meaning once its deployment is gone.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a41f9c7b2e05"
down_revision: str | None = "e8f61ab39c17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_service",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("service_key", sa.String(length=40), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("tls_enabled", sa.Boolean(), nullable=False),
        sa.Column("ca_cert_pem", sa.Text(), nullable=True),
        sa.Column("username", sa.String(length=200), nullable=False),
        sa.Column("password_secret_name", sa.String(length=200), nullable=False),
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
            "service_key IN ('mqtt')",
            name=op.f("ck_deployment_service_service_key_vocab"),
        ),
        sa.CheckConstraint(
            "port > 0 AND port < 65536",
            name=op.f("ck_deployment_service_port_range"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployment.id"],
            name=op.f("fk_deployment_service_deployment_id_deployment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployment_service")),
        sa.UniqueConstraint(
            "deployment_id",
            "service_key",
            name=op.f("uq_deployment_service_deployment_id"),
        ),
    )
    op.create_index(
        op.f("ix_deployment_service_deployment_id"),
        "deployment_service",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_deployment_service_deployment_id"), table_name="deployment_service")
    op.drop_table("deployment_service")
