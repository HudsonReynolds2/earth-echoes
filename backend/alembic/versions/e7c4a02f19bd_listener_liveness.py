"""listener_liveness

Revision ID: e7c4a02f19bd
Revises: d3b1a7f45e92
Create Date: 2026-08-10

Task E3.9 (spec 6.5, 7.3, 9.3). The spec 6.5 liveness block, persisted on
`device_state` - which is where E3.5's docstring said it would land, and
correctly so: unlike the E3.8 LWT verdict, liveness arrives INSIDE a
`lst/{mac}/reported` payload. It is something the device said, on the row
that holds what the device said.

Four nullable columns and three CHECK constraints. The columns are nullable
because an Aggregator's row has no spec 6.5 liveness at all - its verdict is
the LWT one in `aggregator_status` - and because a Listener that has not
reported yet is not the same as one reported offline.

The constraints repeat rules the wire contract already enforces
(`contracts.mqtt.ListenerLiveness`). That duplication is deliberate: Pydantic
protects the boundary, Postgres protects the table, and a future writer that
is not the MQTT consumer will meet the second one.

Nothing here computes anything. The Aggregator tracks wake windows, applies
`listener.wake_grace_seconds` and raises `listener_missed_wake_window`; the
platform records the outcome. A backfill would have to invent a liveness
state for every existing Listener row, so there is none - NULL is the honest
value for "no Listener report has arrived since this shipped".

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7c4a02f19bd"
down_revision: str | None = "d3b1a7f45e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device_state", sa.Column("liveness_state", sa.String(length=20), nullable=True))
    op.add_column(
        "device_state", sa.Column("last_audio_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "device_state", sa.Column("expected_wake_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "device_state",
        sa.Column("liveness_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "liveness_state_vocab",
        "device_state",
        "liveness_state IS NULL OR liveness_state IN ('streaming','sleeping','offline')",
    )
    op.create_check_constraint(
        "wake_time_belongs_to_sleeping",
        "device_state",
        "(liveness_state = 'sleeping') = (expected_wake_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "liveness_is_listener_only",
        "device_state",
        "entity_type = 'listener' OR liveness_state IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_device_state_liveness_is_listener_only"), "device_state", type_="check"
    )
    op.drop_constraint(
        op.f("ck_device_state_wake_time_belongs_to_sleeping"), "device_state", type_="check"
    )
    op.drop_constraint(op.f("ck_device_state_liveness_state_vocab"), "device_state", type_="check")
    op.drop_column("device_state", "liveness_changed_at")
    op.drop_column("device_state", "expected_wake_at")
    op.drop_column("device_state", "last_audio_at")
    op.drop_column("device_state", "liveness_state")
