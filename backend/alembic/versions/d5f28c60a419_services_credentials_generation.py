"""services_credentials_generation

Revision ID: d5f28c60a419
Revises: c4e9b21f83da
Create Date: 2026-08-12

Task E5.11 (spec 16.3, 16.4; DECISIONS D134). One column and one catalog key:
the count of how many times a deployment's service credentials have been
generated.

**Why a rotation needs a counter at all.** A device's desired config snapshot
carries secret MARKERS and never plaintext (spec 5.4 and 8; D51, D126), and a
marker is a SecretStore NAME -- `{"$secret": "config:deployment:<id>:
telemetry.influx_token"}` -- which is the identical string before and after a
rotation. So rotating every credential a deployment has changes nothing in any
device's snapshot: every plan entry is a no-op, no revision is minted, and no
device is told anything. The phase document's E5.11 acceptance asks rotation to
produce one revision per Aggregator precisely so that "rotation is a config
revision, not a manual redistribution" (spec 16.3), and without this column that
sentence is not true of any device.

Measured before it was believed: rotating with an unchanged hostname minted zero
revisions, and rotating to a DIFFERENT hostname minted one per Aggregator and
none per Listener -- so the projection path was working and the marker
convention was behaving exactly as designed. The counter is the non-secret thing
that changes.

**A count and not a timestamp.** Two renders of one generation have to be
byte-identical (fixed choice 7), and a clock is not. It is also what a device
can compare cheaply against what it last acted on.

`services.credentials_generation` is `write_restricted=SERVICE_ONBOARDING` like
the other twelve, so operators cannot set it and Listener snapshots exclude it
(spec 5.4) -- which is what keeps "zero revisions per Listener" true.

Reversible: `downgrade` drops the column. A deployment that has rotated and is
then downgraded loses only the platform's count; the credentials themselves are
in SecretStore and on the operator's stack, unaffected. Re-upgrading restarts
the count at 0, which would make the next rotation look like the first to a
device that had cached a higher number -- stated here rather than guarded,
because the counter is a change signal and a device that sees an unexpected
value re-fetches, which is the safe direction.
"""

import sqlalchemy as sa

from alembic import op

revision = "d5f28c60a419"
down_revision = "c4e9b21f83da"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment",
        sa.Column(
            "services_credentials_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("deployment", "services_credentials_generation")
