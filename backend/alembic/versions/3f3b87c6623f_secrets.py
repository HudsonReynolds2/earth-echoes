"""secrets

Revision ID: 3f3b87c6623f
Revises: c872acca01ec
Create Date: 2026-07-24 15:47:14.372406

Reversibility is mandatory: downgrade() must fully undo upgrade()
(docs/migration-conventions.md). Autogenerate output is a draft; review it
by hand before committing.
"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401

from alembic import op  # noqa: F401

revision: str = "3f3b87c6623f"
down_revision: str | None = "c872acca01ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("dek_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("kek_fingerprint", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret")),
    )
    op.create_index(op.f("ix_secret_name"), "secret", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_secret_name"), table_name="secret")
    op.drop_table("secret")
