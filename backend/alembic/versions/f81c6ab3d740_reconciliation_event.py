"""reconciliation_event

Revision ID: f81c6ab3d740
Revises: e7c4a02f19bd
Create Date: 2026-08-10

Task E3.11 (spec 6.3). One row per spec 6.2 transition: timestamp, actor
(user or system), the before/after effective-config diff, and any
device-supplied detail. Written by `revision_state.transition` and nothing
else, which is what makes the timeline complete by construction - every state
change in the system already passes through that one function.

Append-only evidence, so every reference out of it is deliberately un-FK'd on
the D33 precedent: a transition has to survive both the revision it describes
being pruned and the device being decommissioned. The timeline exists to
answer questions about things that are gone.

Not backfilled. The transitions that happened before this migration were
never recorded, and inventing rows for them would put a fabricated history on
a screen an operator is meant to trust. Timelines start empty and fill.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f81c6ab3d740"
down_revision: str | None = "e7c4a02f19bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("from_state", sa.String(length=20), nullable=False),
        sa.Column("to_state", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=40), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('aggregator','listener')",
            name=op.f("ck_reconciliation_event_target_type_vocab"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_event")),
    )
    op.create_index(
        op.f("ix_reconciliation_event_revision_id"),
        "reconciliation_event",
        ["revision_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_reconciliation_event_at"), "reconciliation_event", ["at"], unique=False
    )
    op.create_index(
        "ix_reconciliation_event_timeline",
        "reconciliation_event",
        ["target_type", "target_id", "at"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_event_scope",
        "reconciliation_event",
        ["deployment_id", "at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_event_scope", table_name="reconciliation_event")
    op.drop_index("ix_reconciliation_event_timeline", table_name="reconciliation_event")
    op.drop_index(op.f("ix_reconciliation_event_at"), table_name="reconciliation_event")
    op.drop_index(op.f("ix_reconciliation_event_revision_id"), table_name="reconciliation_event")
    op.drop_table("reconciliation_event")
