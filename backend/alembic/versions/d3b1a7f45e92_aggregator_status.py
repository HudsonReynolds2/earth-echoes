"""aggregator_status

Revision ID: d3b1a7f45e92
Revises: c41e9b7d3a58
Create Date: 2026-08-10

Task E3.8 (spec 9.3, 7.2, 7.3). The Aggregator's live online verdict, which
spec 9.3 makes MQTT's to decide and explicitly not Prometheus's: the
remote-write agent buffers to a write-ahead log and backfills on reconnect
(spec 10.4), so central Prometheus lags real time by design.

A table of its own rather than columns on `device_state`, though E3.5's
docstring anticipated the latter. `device_state` is "the last state the device
REPORTED" with `reported_at`, `checksum` and `config` NOT NULL, and a device
publishes `online` before it has ever reported a config - so a status-only row
would need three of those columns made nullable, dissolving the invariant that
a `device_state` row is a report. LWT is also Aggregator-only (spec 6.4:
Listeners hold no MQTT session), and an `offline` LWT is published by the
BROKER on the device's behalf, which is precisely the state the device did not
send.

`declared_at` holds the payload's `at` and is compared against nothing.
A device composes its will at CONNECT time, so an LWT's `at` is older than
every `online` that followed it; ordering status by it - the way spec 7.4
orders reports - would reject every LWT as stale and leave dead devices
reading online forever. Receipt order decides, and `changed_at` records when
the verdict last actually moved.

The FK cascades, unlike `device_state.entity_id` which is deliberately
un-FK'd: there is one target table here rather than two, and this is current
state rather than evidence, so it dies with its device. `device_event` keeps
the history.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3b1a7f45e92"
down_revision: str | None = "c41e9b7d3a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "aggregator_status",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregator_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("online", sa.Boolean(), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["aggregator_id"],
            ["aggregator.id"],
            name=op.f("fk_aggregator_status_aggregator_id_aggregator"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployment.id"],
            name=op.f("fk_aggregator_status_deployment_id_deployment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_aggregator_status")),
        sa.UniqueConstraint("aggregator_id", name=op.f("uq_aggregator_status_aggregator_id")),
    )
    op.create_index(
        op.f("ix_aggregator_status_deployment_id"),
        "aggregator_status",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_aggregator_status_deployment_id"), table_name="aggregator_status")
    op.drop_table("aggregator_status")
