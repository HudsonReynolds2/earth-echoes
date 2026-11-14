"""map_coordinates

Revision ID: 670aec9e9363
Revises: 48467572e941
Create Date: 2026-11-13

Task E6.2a (spec 9.2; phase-6 fixed choice 2; DECISIONS D189). Nullable
`gps_lat`/`gps_lon` on `aggregator` (the Pod marker sits at its Aggregator)
and `deployment` (its representative coordinates). No derived fallback: NULL
means "not placed yet".

Two named CHECKs per table, which autogenerate does not detect and were added
by hand: `gps_pair` (both or neither) and `gps_range` (latitude and longitude
in range). They state the same rule the API enforces with a 422, so no path
around the API (import, seed, a future tool) can store half a pair. The
expressions come from `models.GPS_PAIR_CHECK`/`GPS_RANGE_CHECK`, so the
constraint and the model cannot disagree.

Reversible: `downgrade` drops the checks, then the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "670aec9e9363"
down_revision: str | None = "48467572e941"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported: a migration must not change when the model module
# later does (migration convention 1). Equal to app.models today.
GPS_PAIR_CHECK = "(gps_lat IS NULL) = (gps_lon IS NULL)"
GPS_RANGE_CHECK = "gps_lat IS NULL OR (gps_lat BETWEEN -90 AND 90 AND gps_lon BETWEEN -180 AND 180)"

TABLES = ("aggregator", "deployment")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("gps_lat", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("gps_lon", sa.Float(), nullable=True))
        op.create_check_constraint(op.f(f"ck_{table}_gps_pair"), table, GPS_PAIR_CHECK)
        op.create_check_constraint(op.f(f"ck_{table}_gps_range"), table, GPS_RANGE_CHECK)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_constraint(op.f(f"ck_{table}_gps_range"), table, type_="check")
        op.drop_constraint(op.f(f"ck_{table}_gps_pair"), table, type_="check")
        op.drop_column(table, "gps_lon")
        op.drop_column(table, "gps_lat")
