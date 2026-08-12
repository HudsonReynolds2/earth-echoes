"""service_required_flag

Revision ID: b7d41f0c2e93
Revises: a31287354e23
Create Date: 2026-08-12

Task E5.5 (spec 16.2, 16.5; phase-5 fixed choice 2). One boolean:
`deployment_service.required`, whether this service has to reach `verified` for
its DEPLOYMENT to.

**Why it is a stored column and not an argument to `roll_up`** (D117). Spec
16.2 makes object storage conditionally required, so "is this service required"
is a real per-deployment fact, and E5.4e's tester is what discovers it -- it
answers `not_required` when raw-audio upload is off. That answer arrives during
a test run. But `roll_up` is recomputed on paths that have no test results in
hand at all: `PUT /deployments/{id}/services` recomputes after a save, and the
suite-wide invariant walks every deployment and recomputes from its rows alone.
A rollup that depended on a fact known only while a test was running could not
be reproduced by either of them, and the stored `deployment.services_status`
would silently drift from its own definition -- which is the single risk that
denormalizing the column takes on, and the thing fixed choice 2 promises to
answer by construction rather than by argument.

So the tester's answer is persisted here on the row, and `roll_up` stays a pure
function of the rows it is given.

Default `true`: every service is required until something says otherwise, which
is the safe direction. A service wrongly marked required holds a deployment at
`pending_verification` and the operator sees exactly which one; a service
wrongly marked not-required lets a deployment reach `verified` while a store it
depends on is unreachable.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d41f0c2e93"
down_revision: str | None = "a31287354e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployment_service",
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("deployment_service", "required")
