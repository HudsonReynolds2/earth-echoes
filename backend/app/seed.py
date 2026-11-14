"""Dev seed (task E0.12): fresh environment to logged-in owner in ONE command.

    uv run python -m app.seed          # owner only (E0.12 behavior, unchanged)
    uv run python -m app.seed --demo   # owner + the canonical demo hierarchy

Runs migrations to head, then creates the initial organization-wide owner
account. The password is generated, printed EXACTLY ONCE to stdout, and never
stored anywhere except as an Argon2id hash (phase-0 acceptance; rule R2).
Refuses to run if an org-wide owner already exists — unless --demo is given
and only the hierarchy is missing, in which case it adds the hierarchy alone
(task E1.9; DECISIONS D43). Refuses to re-seed a demo hierarchy that already
exists. Override the email with EOE_SEED_OWNER_EMAIL (default
owner@example.com).

The demo fixture is CANONICAL and deterministic (documented by name in
docs/INTERFACES.md; the frontend test fixture mirrors it): E2 and E6 sessions
reference these rows by name.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import command
from app.audit import record_audit
from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.models import Aggregator, Deployment, Listener, Organization, Pod, RoleAssignment, User
from app.settings import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_EMAIL = "owner@example.com"

DEMO_ORG = "Earth Echoes Demo"

# (deployment name, slug, tags, [(pod name, pod tags, aggregator_uuid,
#  listener count, listener name prefix, MAC block)])
DEMO_HIERARCHY = [
    (
        "Redwood Coast",
        "redwood-coast",
        ["coastal"],
        [
            (
                "Pod 01 · Alder Creek",
                ["coastal"],
                "demo-agg-rc-01",
                8,
                "alder-creek",
                "02:EE:0E:01:01",
            ),
            ("Pod 02 · Ridge Line", ["ridge"], "demo-agg-rc-02", 5, "ridge-line", "02:EE:0E:01:02"),
            ("Pod 03 · Tarn Meadow", [], "demo-agg-rc-03", 3, "tarn-meadow", "02:EE:0E:01:03"),
        ],
    ),
    (
        "High Desert",
        "high-desert",
        ["ridge"],
        [
            ("Pod 01 · Basin Flat", ["solar"], "demo-agg-hd-01", 6, "basin-flat", "02:EE:0E:02:01"),
            ("Pod 02 · Mesa Rim", [], "demo-agg-hd-02", 4, "mesa-rim", "02:EE:0E:02:02"),
            ("Pod 03 · Dry Wash", [], "demo-agg-hd-03", 2, "dry-wash", "02:EE:0E:02:03"),
        ],
    ),
]


# E6.2a (D189): where the demo sits on the map. Redwood Coast is on the
# northern California coast near Orick; High Desert is in central Oregon near
# Fort Rock. Pods are 1-2 km apart, keyed by aggregator_uuid. Mirrored in
# frontend/tests/inventory-fixture.ts; INTERFACES "The demo fixture" states
# the rule.
DEMO_DEPLOYMENT_COORDS: dict[str, tuple[float, float]] = {
    "redwood-coast": (41.2130, -124.0040),
    "high-desert": (43.3580, -121.0560),
}
DEMO_POD_COORDS: dict[str, tuple[float, float]] = {
    "demo-agg-rc-01": (41.2210, -124.0120),
    "demo-agg-rc-02": (41.2080, -123.9920),
    "demo-agg-rc-03": (41.1990, -124.0100),
    "demo-agg-hd-01": (43.3670, -121.0660),
    "demo-agg-hd-02": (43.3550, -121.0420),
    "demo-agg-hd-03": (43.3460, -121.0700),
}
#: Even-index listener k = index // 2 sits about 130 m north, east, south,
#: then west of its Pod. Odd indices stay GPS-less, so every Pod keeps a mix
#: of pins and leaves (spec 9.2).
DEMO_LEAF_OFFSETS: tuple[tuple[float, float], ...] = (
    (0.0012, 0.0),
    (0.0, 0.0016),
    (-0.0012, 0.0),
    (0.0, -0.0016),
)


def demo_listener_gps(aggregator_uuid: str, index: int) -> tuple[float | None, float | None]:
    """The fixture rule, in one place: even indices carry GPS around their
    Pod, odd indices carry none."""
    if index % 2 != 0:
        return None, None
    pod_lat, pod_lon = DEMO_POD_COORDS[aggregator_uuid]
    dlat, dlon = DEMO_LEAF_OFFSETS[(index // 2) % len(DEMO_LEAF_OFFSETS)]
    return round(pod_lat + dlat, 6), round(pod_lon + dlon, 6)


def seed_demo_hierarchy(db: Session) -> dict[str, int]:
    """Stage the canonical demo hierarchy. Fully deterministic — no randomness
    — so tests and later epics (E2 selections, E6 map) reference rows by name.
    Caller commits."""
    organization = Organization(name=DEMO_ORG)
    db.add(organization)
    db.flush()
    counts = {"deployments": 0, "pods": 0, "aggregators": 0, "listeners": 0}
    for dep_name, slug, dep_tags, pod_rows in DEMO_HIERARCHY:
        dep_lat, dep_lon = DEMO_DEPLOYMENT_COORDS[slug]
        deployment = Deployment(
            organization_id=organization.id,
            name=dep_name,
            slug=slug,
            tags=dep_tags,
            gps_lat=dep_lat,
            gps_lon=dep_lon,
        )
        db.add(deployment)
        db.flush()
        counts["deployments"] += 1
        for pod_name, pod_tags, aggregator_uuid, listener_count, prefix, mac_block in pod_rows:
            pod = Pod(deployment_id=deployment.id, name=pod_name, tags=pod_tags)
            db.add(pod)
            db.flush()
            pod_lat, pod_lon = DEMO_POD_COORDS[aggregator_uuid]
            aggregator = Aggregator(
                pod_id=pod.id, aggregator_uuid=aggregator_uuid, gps_lat=pod_lat, gps_lon=pod_lon
            )
            db.add(aggregator)
            db.flush()
            counts["pods"] += 1
            counts["aggregators"] += 1
            for index in range(listener_count):
                # Mirrors frontend/tests/inventory-fixture.ts exactly: even
                # indices carry GPS around their Pod (E6.2a), the first
                # listener carries the pod tags.
                gps_lat, gps_lon = demo_listener_gps(aggregator_uuid, index)
                db.add(
                    Listener(
                        mac=f"{mac_block}:{index + 1:02d}",
                        name=f"{prefix}-{index + 1:02d}",
                        aggregator_id=aggregator.id,
                        deployment_id=deployment.id,
                        gps_lat=gps_lat,
                        gps_lon=gps_lon,
                        tags=pod_tags if index == 0 else [],
                    )
                )
                counts["listeners"] += 1
    record_audit(
        db,
        action="inventory.seed_demo",
        entity_type="organization",
        entity_id=DEMO_ORG,
        actor_user_id=None,  # system bootstrap
        detail=counts,
    )
    return counts


def migrate_to_head(database_url: str) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["configure_logger"] = False
    os.environ.setdefault("DATABASE_URL", database_url)
    command.upgrade(config, "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.seed")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also seed the canonical demo hierarchy (E1.9); with an existing "
        "owner, seeds the hierarchy alone",
    )
    args = parser.parse_args(argv)

    settings = Settings()  # type: ignore[call-arg]  # resolves from env (D5)
    email = os.environ.get("EOE_SEED_OWNER_EMAIL", DEFAULT_EMAIL)

    print("running migrations to head ...")
    migrate_to_head(settings.database_url)

    _, factory = create_session_factory(settings.database_url)
    with factory() as db:
        if args.demo:
            demo_exists = (
                db.scalar(select(Organization.id).where(Organization.name == DEMO_ORG)) is not None
            )
            if demo_exists:
                print("the demo hierarchy already exists; refusing to seed", file=sys.stderr)
                return 1

        existing = db.scalar(
            select(User)
            .join(RoleAssignment)
            .where(
                RoleAssignment.role == Role.OWNER.value,
                RoleAssignment.deployment_id.is_(None),
            )
        )
        if existing is not None and not args.demo:
            print(
                f"an organization-wide owner already exists ({existing.email}); refusing to seed",
                file=sys.stderr,
            )
            return 1

        password: str | None = None
        if existing is None:
            password = secrets.token_urlsafe(16)
            owner = User(email=email, password_hash=hash_password(password))
            owner.role_assignments.append(RoleAssignment(role=Role.OWNER.value, deployment_id=None))
            db.add(owner)
            db.flush()
            record_audit(
                db,
                action="user.create",
                entity_type="user",
                entity_id=str(owner.id),
                actor_user_id=None,  # system bootstrap
                detail={"email": email, "roles": [Role.OWNER.value], "seed": True},
            )
        if args.demo:
            counts = seed_demo_hierarchy(db)
        db.commit()

    if args.demo:
        print(
            f"demo hierarchy seeded: {counts['deployments']} deployments, "
            f"{counts['pods']} pods, {counts['aggregators']} aggregators, "
            f"{counts['listeners']} listeners"
        )
    if password is not None:
        print()
        print("=" * 62)
        print("  initial owner created — record these credentials NOW;")
        print("  the password is not stored and will never be shown again")
        print("=" * 62)
        print(f"  email:    {email}")
        print(f"  password: {password}")
        print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
