"""services_data_model

Revision ID: a31287354e23
Revises: f81c6ab3d740
Create Date: 2026-08-11

Task E5.1 (spec 16.2, 16.5; phase-5 fixed choices 1 and 2). `deployment_service`
WIDENS to carry all five spec 16.2 services rather than forking into a second
table: `service_key` grows from `('mqtt')` to the five keys, the four
MQTT-shaped connection columns become nullable behind a conditional CHECK that
still makes them mandatory for an `mqtt` row, and two JSONB columns plus the
per-service status block carry everything the other four services need.
`deployment.services_status` is the spec 16.5 rollup; E5.5's `roll_up` is its
only writer and this migration only creates the column and its default.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md), and it does - every column added is dropped
and every CHECK is restored to its pre-E5.1 text.

**A downgrade run against a database that already holds non-`mqtt` service
rows fails, on purpose,** when the narrowed `service_key IN ('mqtt')` CHECK is
restored. The old schema cannot represent four other services' endpoints and
credentials, and deciding what happens to them is an operator's call, not a
migration's - a downgrade that silently deleted them would destroy the only
record of which SecretStore entries were still live.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a31287354e23"
down_revision: str | None = "f81c6ab3d740"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- deployment: the spec 16.5 rollup (fixed choice 2) ---
    op.add_column(
        "deployment",
        sa.Column(
            "services_status",
            sa.String(length=30),
            server_default=sa.text("'unconfigured'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "services_status_vocab",
        "deployment",
        "services_status IN ('unconfigured', 'pending_verification', 'verified', 'degraded')",
    )

    # --- deployment_service: the four non-broker services (fixed choice 1) ---
    op.add_column(
        "deployment_service",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "deployment_service",
        sa.Column(
            "secret_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "deployment_service",
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'untested'"),
            nullable=False,
        ),
    )
    op.add_column("deployment_service", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column(
        "deployment_service",
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deployment_service",
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "deployment_service",
        sa.Column("last_test_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # The broker's four columns become conditionally required rather than
    # unconditionally NOT NULL. Widen first, then re-impose the requirement
    # for `mqtt` only, so no window exists in which an mqtt row could be
    # written without coordinates.
    op.alter_column(
        "deployment_service", "host", existing_type=sa.VARCHAR(length=255), nullable=True
    )
    op.alter_column("deployment_service", "port", existing_type=sa.INTEGER(), nullable=True)
    op.alter_column(
        "deployment_service", "username", existing_type=sa.VARCHAR(length=200), nullable=True
    )
    op.alter_column(
        "deployment_service",
        "password_secret_name",
        existing_type=sa.VARCHAR(length=200),
        nullable=True,
    )
    op.create_check_constraint(
        "mqtt_coordinates_required",
        "deployment_service",
        "service_key <> 'mqtt' OR ("
        "host IS NOT NULL AND port IS NOT NULL "
        "AND username IS NOT NULL AND password_secret_name IS NOT NULL)",
    )

    # The vocabulary itself: five keys, transcribed from the spec 16.2 table.
    op.drop_constraint(
        op.f("ck_deployment_service_service_key_vocab"), "deployment_service", type_="check"
    )
    op.create_check_constraint(
        "service_key_vocab",
        "deployment_service",
        "service_key IN ('mqtt', 'influx', 'prometheus', 'grafana', 's3')",
    )
    op.create_check_constraint(
        "status_vocab",
        "deployment_service",
        "status IN ('untested', 'verified', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_deployment_service_status_vocab"), "deployment_service", type_="check"
    )
    op.drop_constraint(
        op.f("ck_deployment_service_service_key_vocab"), "deployment_service", type_="check"
    )
    # Fails loudly if non-mqtt rows exist; see the module docstring.
    op.create_check_constraint("service_key_vocab", "deployment_service", "service_key IN ('mqtt')")
    op.drop_constraint(
        op.f("ck_deployment_service_mqtt_coordinates_required"), "deployment_service", type_="check"
    )
    op.alter_column(
        "deployment_service",
        "password_secret_name",
        existing_type=sa.VARCHAR(length=200),
        nullable=False,
    )
    op.alter_column(
        "deployment_service", "username", existing_type=sa.VARCHAR(length=200), nullable=False
    )
    op.alter_column("deployment_service", "port", existing_type=sa.INTEGER(), nullable=False)
    op.alter_column(
        "deployment_service", "host", existing_type=sa.VARCHAR(length=255), nullable=False
    )
    op.drop_column("deployment_service", "last_test_detail")
    op.drop_column("deployment_service", "consecutive_failures")
    op.drop_column("deployment_service", "last_tested_at")
    op.drop_column("deployment_service", "status_reason")
    op.drop_column("deployment_service", "status")
    op.drop_column("deployment_service", "secret_names")
    op.drop_column("deployment_service", "config")
    op.drop_constraint(op.f("ck_deployment_services_status_vocab"), "deployment", type_="check")
    op.drop_column("deployment", "services_status")
