"""reconciliation_policy_and_published_at

Revision ID: c41e9b7d3a58
Revises: a2cf00fc037f
Create Date: 2026-08-10

Task E3.7 (spec 6.2, 6.4). Two additions, both of them lifecycle state the
reconciliation worker must find in Postgres rather than in its own memory,
because the phase acceptance is that a restarted worker loses nothing
(spec 14.3):

- `deployment.pending_timeout_seconds` / `deployment.auto_reconcile`. The
  spec 6.4 item 4 window is "configurable per deployment" and is a PLATFORM
  setting, not a device setting, so it belongs on the deployment row and not
  in the E2 settings catalog - nothing about it may ever reach a device.
  `auto_reconcile` is stored, defaults off and is inert pending spec 17
  item 3; the column exists so the decision has somewhere to land.
- `config_revision.published_at`. The timeout is measured from the moment a
  revision ENTERED `pending`, which `created_at` cannot supply: an operator
  retrying a `failed` revision an hour after it was drafted starts a fresh
  window, and measuring from `created_at` would fail it again immediately.

`published_at` is nullable and NOT backfilled. Every revision that exists
when this runs is a `draft` E2 wrote (D55) or one E3.4 published in a test
database; a backfill would have to invent a publication instant, and the
worker's rule is that a `pending` revision with no `published_at` is left
alone and reported, never timed out on a guessed clock.

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c41e9b7d3a58"
down_revision: str | None = "a2cf00fc037f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployment",
        sa.Column(
            "pending_timeout_seconds",
            sa.Integer(),
            server_default="300",
            nullable=False,
        ),
    )
    op.add_column(
        "deployment",
        sa.Column(
            "auto_reconcile",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_deployment_pending_timeout_positive"),
        "deployment",
        "pending_timeout_seconds > 0",
    )
    op.add_column(
        "config_revision",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_config_revision_published_at"),
        "config_revision",
        ["published_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_config_revision_published_at"), table_name="config_revision")
    op.drop_column("config_revision", "published_at")
    op.drop_constraint(op.f("ck_deployment_pending_timeout_positive"), "deployment", type_="check")
    op.drop_column("deployment", "auto_reconcile")
    op.drop_column("deployment", "pending_timeout_seconds")
