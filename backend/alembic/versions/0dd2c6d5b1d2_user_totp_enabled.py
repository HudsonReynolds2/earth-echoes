"""user_totp_enabled

Revision ID: 0dd2c6d5b1d2
Revises: 3f3b87c6623f
Create Date: 2026-07-24 15:58:42.093111

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). Autogenerate output is a draft; review it
by hand before committing.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401

from alembic import op  # noqa: F401

revision: str = "0dd2c6d5b1d2"
down_revision: str | None = "3f3b87c6623f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-adjusted from autogenerate: NOT NULL needs a server_default to
    # succeed on tables that already hold rows (migration-conventions.md).
    op.add_column(
        "user",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("user", "totp_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("user", "totp_enabled")
