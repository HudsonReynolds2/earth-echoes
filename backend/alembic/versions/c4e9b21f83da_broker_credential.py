"""broker_credential

Revision ID: c4e9b21f83da
Revises: b7d41f0c2e93
Create Date: 2026-08-12

Task E5.6 (spec 7.1, 7.2, 16.4; phase-5 fixed choices 4 and 6). One table: the
platform's record of the per-device broker logins it minted through Mosquitto's
dynamic security plugin.

**Why `aggregator_uuid` is a bare string and not a foreign key.** The row has to
outlive the device. Deleting an Aggregator is exactly the moment its broker
login must be destroyed, and if the broker is unreachable then the destruction
has to be retried later -- so a cascade from `aggregator` would delete the
platform's only record of a login that is still live on somebody's broker. Spec
4.2 also makes `aggregator_uuid` the identifier the device itself carries and
the one the broker knows it by, so this column is the join that matters here.

**Three states, where the phase document pencilled two** (project-changes #27,
DECISIONS D121). `minted` and `revoked` are the steady ones. `revoke_pending`
is the owner's answer, taken on 2026-08-12, to a question the document left
implicit: what happens when an operator deletes an Aggregator whose broker is
down. Refusing the delete would let one deployment's outage block inventory
work; letting the delete pass silently would leave a decommissioned Pi holding a
working credential forever. So the delete proceeds, the row lands here, and
`app/services/credentials.py::drain_pending_revocations` retries on the worker's
sweep until the broker confirms.

The `revoked_at_matches_state` CHECK is what stops that third state from rotting
into ambiguity: `revoked_at` means "the broker confirmed", on every row, rather
than "we asked" on some of them.

Reversible: `downgrade` drops the table. The credentials themselves live on the
broker and in SecretStore, so a downgrade loses the platform's *record* of them
-- which is the same exposure every other table in this chain has and is why
downgrades are a development tool here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e9b21f83da"
down_revision: str | None = "b7d41f0c2e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("aggregator_uuid", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=200), nullable=False),
        sa.Column("password_secret_name", sa.String(length=200), nullable=False),
        sa.Column(
            "state", sa.String(length=20), nullable=False, server_default=sa.text("'minted'")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployment.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "aggregator_uuid"),
        sa.CheckConstraint(
            "state IN ('minted', 'revoke_pending', 'revoked')",
            name="state_vocab",
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (revoked_at IS NOT NULL)",
            name="revoked_at_matches_state",
        ),
    )
    op.create_index(
        op.f("ix_broker_credential_deployment_id"),
        "broker_credential",
        ["deployment_id"],
    )
    op.create_index(
        op.f("ix_broker_credential_aggregator_uuid"),
        "broker_credential",
        ["aggregator_uuid"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_broker_credential_aggregator_uuid"), table_name="broker_credential")
    op.drop_index(op.f("ix_broker_credential_deployment_id"), table_name="broker_credential")
    op.drop_table("broker_credential")
