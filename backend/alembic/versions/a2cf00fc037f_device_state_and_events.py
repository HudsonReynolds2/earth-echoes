"""device_state_and_events

Revision ID: a2cf00fc037f
Revises: a41f9c7b2e05
Create Date: 2026-08-10

Task E3.5 (spec 6.1, 7.3, 7.4). The two things the reported consumer
persists: `device_state`, spec 6.1's "last state the device sent", one row per
device replaced in place; and `device_event`, the spec 7.3 event stream as
immutable evidence.

Two shapes here are load-bearing rather than incidental:

- `device_state.deployment_id` IS a real foreign key, because current state
  has no meaning once its deployment is gone — the `deployment_service`
  precedent, not the D33 evidence precedent. `device_event`'s is un-FK'd for
  the opposite reason: an event must outlive the device it describes.
- `uq_device_event_delivery` is `NULLS NOT DISTINCT`. Without it Postgres
  treats every Aggregator-level event (`listener_mac` NULL) as unique and the
  index would dedupe only Listener events, so a QoS 1 redelivery would show up
  twice on the E3.11 timeline. Requires Postgres 15+; the stack is on 16.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a2cf00fc037f"
down_revision: str | None = "a41f9c7b2e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("aggregator_uuid", sa.String(length=64), nullable=False),
        sa.Column("listener_mac", sa.String(length=17), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN ('debug','info','warn','error')",
            name=op.f("ck_device_event_level_vocab"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_event")),
    )
    op.create_index(
        op.f("ix_device_event_deployment_id"), "device_event", ["deployment_id"], unique=False
    )
    op.create_index(
        op.f("ix_device_event_listener_mac"), "device_event", ["listener_mac"], unique=False
    )
    op.create_index(
        "ix_device_event_timeline", "device_event", ["aggregator_uuid", "at"], unique=False
    )
    op.create_index(
        "uq_device_event_delivery",
        "device_event",
        ["deployment_id", "aggregator_uuid", "listener_mac", "at", "code"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_table(
        "device_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_revision_id", sa.Uuid(), nullable=True),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("health", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('aggregator','listener')",
            name=op.f("ck_device_state_entity_type_vocab"),
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployment.id"],
            name=op.f("fk_device_state_deployment_id_deployment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_state")),
        sa.UniqueConstraint("entity_type", "entity_id", name=op.f("uq_device_state_entity_type")),
    )
    op.create_index(
        op.f("ix_device_state_deployment_id"), "device_state", ["deployment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_device_state_deployment_id"), table_name="device_state")
    op.drop_table("device_state")
    op.drop_index(
        "uq_device_event_delivery", table_name="device_event", postgresql_nulls_not_distinct=True
    )
    op.drop_index("ix_device_event_timeline", table_name="device_event")
    op.drop_index(op.f("ix_device_event_listener_mac"), table_name="device_event")
    op.drop_index(op.f("ix_device_event_deployment_id"), table_name="device_event")
    op.drop_table("device_event")
