"""baseline

Revision ID: 4a07fe3a8e54
Revises:
Create Date: 2026-07-23 23:52:14.704672

Root of the migration chain. Deliberately empty: it exists so every real
migration has a parent and `downgrade base` has a defined floor. This is the
single migration exempt from the non-trivial-downgrade rule
(docs/migration-conventions.md); every later revision must fully reverse
itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401

from alembic import op  # noqa: F401

revision: str = "4a07fe3a8e54"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
