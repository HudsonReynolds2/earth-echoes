"""listener_gps_checks

Revision ID: 1d73e4435c21
Revises: 670aec9e9363
Create Date: 2026-11-14

Hygiene task H1 (DECISIONS D199; project-changes #50). Listeners get the same
two named CHECKs E6.2a gave Deployments and Aggregators: `gps_pair` (both or
neither) and `gps_range` (latitude and longitude in range). The columns have
existed since E1, and the API refused out-of-range values but accepted half a
pair until H1. There is no data-repair step: the owner confirmed on 2026-11-14
that no install holds data that matters, so an install with a half row fails
this upgrade loudly instead of having its data silently changed.

Reversible: `downgrade` drops the two checks.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1d73e4435c21"
down_revision: str | None = "670aec9e9363"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported (migration convention 1). Equal to app.models today.
GPS_PAIR_CHECK = "(gps_lat IS NULL) = (gps_lon IS NULL)"
GPS_RANGE_CHECK = "gps_lat IS NULL OR (gps_lat BETWEEN -90 AND 90 AND gps_lon BETWEEN -180 AND 180)"


def upgrade() -> None:
    op.create_check_constraint(op.f("ck_listener_gps_pair"), "listener", GPS_PAIR_CHECK)
    op.create_check_constraint(op.f("ck_listener_gps_range"), "listener", GPS_RANGE_CHECK)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_listener_gps_range"), "listener", type_="check")
    op.drop_constraint(op.f("ck_listener_gps_pair"), "listener", type_="check")
