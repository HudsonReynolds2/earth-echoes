"""E6.4: the marker rollup and the map's reads (spec 9.2, 9.3, 14.4, 16.5; D190, D191).

Four properties:

1. **The rollup is worst-of-descendants and nothing else** (table-driven,
   pure): ordering comes from `device_status.SEVERITY`; services `degraded`, and
   only `degraded`, adds a degraded voice; counts cover every status, zeros
   included.
2. **Alerts make a device `alerting` everywhere at once.** An open
   `duplicate_identity` alert on a Listener shows on the map AND in the
   inventory table, because both read `hierarchy_common`'s readers (D60). An
   alert naming no device counts toward its Deployment's badge and changes no
   status.
3. **Scoping:** a Deployment Operator sees only their Deployment; the other
   one is a 404 (D35).
4. **The query count is constant in the number of devices** (phase-6 fixed
   choice 5), from one Listener to a thousand.
"""

import itertools
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.controlplane.device_status import SEVERITY, DeviceStatus, with_alerts
from app.main import API_PREFIX, create_app
from app.map.rollup import roll_up
from app.map.service import deployment_detail, deployments_layer
from app.models import (
    Aggregator,
    AggregatorStatus,
    Deployment,
    InventoryAlert,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.seed import seed_demo_hierarchy
from app.settings import Settings

S = DeviceStatus

# --- 1. The rollup, pure ------------------------------------------------------


@pytest.mark.parametrize(("worse", "better"), list(itertools.combinations(SEVERITY, 2)))
def test_every_severity_pair_rolls_up_to_the_worse(worse, better):
    """`SEVERITY` is worst-first, so every earlier member beats every later one,
    in either order of arrival."""
    assert roll_up([better, worse]).status == worse
    assert roll_up([worse, better]).status == worse


def test_nothing_known_is_unknown_and_nothing_at_all_is_unknown():
    assert roll_up([S.UNKNOWN, S.UNKNOWN]).status == S.UNKNOWN
    assert roll_up([]).status == S.UNKNOWN


def test_one_silent_device_does_not_hide_a_healthy_fleet():
    """`unknown` sits at the bottom (D60), so ten healthy devices and one that
    never spoke read healthy, with the silent one still counted."""
    marker = roll_up([S.HEALTHY] * 10 + [S.UNKNOWN])
    assert marker.status == S.HEALTHY
    assert marker.counts["unknown"] == 1 and marker.counts["healthy"] == 10


def test_a_sleeping_pod_is_sleeping_not_degraded():
    """Spec 6.5: a duty-cycled Pod at night is silent by design."""
    assert roll_up([S.SLEEPING, S.SLEEPING, S.HEALTHY]).status == S.SLEEPING


@pytest.mark.parametrize(
    ("services", "expected"),
    [
        (None, S.HEALTHY),
        ("unconfigured", S.HEALTHY),
        ("pending_verification", S.HEALTHY),
        ("verified", S.HEALTHY),
        ("degraded", S.DEGRADED),
    ],
)
def test_only_degraded_services_move_the_marker(services, expected):
    """D190: a new Deployment is not a broken one."""
    assert roll_up([S.HEALTHY], services_status=services).status == expected


def test_services_degradation_colors_but_is_not_a_device():
    marker = roll_up([S.OFFLINE], services_status="degraded")
    assert marker.status == S.OFFLINE  # offline outranks degraded
    assert sum(marker.counts.values()) == 1
    assert roll_up([], services_status="degraded").status == S.DEGRADED


def test_counts_cover_every_status_in_severity_order():
    counts = roll_up([S.DRIFTED]).counts
    assert list(counts) == [status.value for status in SEVERITY]
    assert counts == {**{status.value: 0 for status in SEVERITY}, "drifted": 1}


@pytest.mark.parametrize("status", list(DeviceStatus))
def test_an_open_alert_beats_every_status(status):
    assert with_alerts(status, 1) == S.ALERTING
    assert with_alerts(status, 0) == status


# --- 2-4. The endpoints --------------------------------------------------------

OWNER = "map-owner@example.com"
OPERATOR = "map-operator@example.com"


@pytest.fixture(scope="module")
def app():
    with ephemeral_postgres() as url:
        application = create_app(
            Settings(
                database_url=url,
                session_secret="e6-map",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
            )
        )
        with application.state.session_factory() as db:
            seed_demo_hierarchy(db)
            db.flush()
            redwood = db.scalars(select(Deployment).where(Deployment.slug == "redwood-coast")).one()
            owner = User(email=OWNER, password_hash=hash_password(PASSWORD))
            owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
            operator = User(email=OPERATOR, password_hash=hash_password(PASSWORD))
            operator.role_assignments.append(
                RoleAssignment(role="deployment_operator", deployment_id=redwood.id)
            )
            db.add_all([owner, operator])
            db.commit()
        yield application


@pytest.fixture
def clean(app):
    yield app
    with app.state.session_factory() as db:
        db.execute(delete(InventoryAlert))
        db.execute(delete(AggregatorStatus))
        for row in db.scalars(select(Deployment)):
            row.services_status = "unconfigured"
        db.commit()


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def ids(app) -> dict[str, uuid.UUID]:
    with app.state.session_factory() as db:
        found = {row.slug: row.id for row in db.scalars(select(Deployment))}
        found |= {row.aggregator_uuid: row.id for row in db.scalars(select(Aggregator))}
    return found


@pytest.mark.integration
def test_the_organization_layer_rolls_up_a_quiet_fleet_honestly(clean):
    """Nothing has spoken, so every device is `unknown` and so is every
    Deployment. No colour is invented (D40/D60)."""
    rows = login(clean, OWNER).get(f"{API_PREFIX}/map/deployments").json()
    by_slug = {row["slug"]: row for row in rows}
    assert set(by_slug) == {"redwood-coast", "high-desert"}
    redwood = by_slug["redwood-coast"]
    assert redwood["status"] == "unknown"
    assert (redwood["gps_lat"], redwood["gps_lon"]) == (41.213, -124.004)
    assert redwood["pod_count"] == 3 and redwood["listener_count"] == 16
    # 3 Aggregators + 16 Listeners, all unknown.
    assert redwood["counts"]["unknown"] == 19 and redwood["alert_count"] == 0
    assert redwood["services_status"] == "unconfigured"


@pytest.mark.integration
def test_an_offline_aggregator_turns_its_pod_and_deployment_offline(clean):
    from app.models import utcnow

    agg = ids(clean)["demo-agg-rc-02"]
    with clean.state.session_factory() as db:
        now = utcnow()
        db.add(
            AggregatorStatus(
                aggregator_id=agg,
                deployment_id=ids(clean)["redwood-coast"],
                online=False,
                declared_at=now,
                changed_at=now,
            )
        )
        db.commit()
    owner = login(clean, OWNER)
    redwood = ids(clean)["redwood-coast"]
    detail = owner.get(f"{API_PREFIX}/map/deployments/{redwood}").json()
    pods = {pod["name"]: pod for pod in detail["pods"]}
    ridge = pods["Pod 02 · Ridge Line"]
    assert ridge["aggregator_status"] == "offline" and ridge["status"] == "offline"
    assert (ridge["gps_lat"], ridge["gps_lon"]) == (41.208, -123.992)
    assert pods["Pod 01 · Alder Creek"]["status"] == "unknown"
    assert detail["deployment"]["status"] == "offline"
    assert detail["deployment"]["counts"]["offline"] == 1


@pytest.mark.integration
def test_an_alert_makes_a_listener_alerting_on_the_map_and_in_inventory(clean):
    """THE D60/D191 property: one derivation, so the two surfaces agree."""
    mac = "02:EE:0E:01:01:03"
    redwood = ids(clean)["redwood-coast"]
    with clean.state.session_factory() as db:
        db.add(
            InventoryAlert(
                alert_type="duplicate_identity",
                entity_type="listener",
                entity_key=mac,
                deployment_id=redwood,
            )
        )
        db.commit()
    owner = login(clean, OWNER)
    detail = owner.get(f"{API_PREFIX}/map/deployments/{redwood}").json()
    listener = next(row for row in detail["listeners"] if row["mac"] == mac)
    assert listener["status"] == "alerting" and listener["alert_count"] == 1
    pod = next(row for row in detail["pods"] if row["name"] == "Pod 01 · Alder Creek")
    assert pod["status"] == "alerting" and pod["alert_count"] == 1
    assert detail["deployment"]["status"] == "alerting"
    assert detail["deployment"]["alert_count"] == 1
    # The inventory table reads the same derivation.
    assert owner.get(f"{API_PREFIX}/listeners/{mac}").json()["status"] == "alerting"


@pytest.mark.integration
def test_a_resolved_alert_no_longer_counts(clean):
    mac = "02:EE:0E:01:01:03"
    from app.models import utcnow

    with clean.state.session_factory() as db:
        db.add(
            InventoryAlert(
                alert_type="duplicate_identity",
                entity_type="listener",
                entity_key=mac,
                resolved_at=utcnow(),
            )
        )
        db.commit()
    assert login(clean, OWNER).get(f"{API_PREFIX}/listeners/{mac}").json()["status"] == "unknown"


@pytest.mark.integration
def test_an_alert_naming_no_device_badges_the_deployment_only(clean):
    """A `provisioning_required` for an Aggregator nobody entered has no marker
    to colour, so it counts toward its Deployment's badge and moves no status."""
    redwood = ids(clean)["redwood-coast"]
    with clean.state.session_factory() as db:
        db.add(
            InventoryAlert(
                alert_type="provisioning_required",
                entity_type="aggregator",
                entity_key="never-entered-0001",
                deployment_id=redwood,
            )
        )
        db.commit()
    row = next(
        row
        for row in login(clean, OWNER).get(f"{API_PREFIX}/map/deployments").json()
        if row["slug"] == "redwood-coast"
    )
    assert row["alert_count"] == 1 and row["status"] == "unknown"


@pytest.mark.integration
def test_degraded_services_degrade_the_deployment_marker(clean):
    with clean.state.session_factory() as db:
        db.get(Deployment, ids(clean)["high-desert"]).services_status = "degraded"
        db.commit()
    rows = {
        row["slug"]: row for row in login(clean, OWNER).get(f"{API_PREFIX}/map/deployments").json()
    }
    assert rows["high-desert"]["status"] == "degraded"
    assert rows["high-desert"]["services_status"] == "degraded"
    assert rows["redwood-coast"]["status"] == "unknown"


@pytest.mark.integration
def test_listeners_carry_their_pod_and_position_or_none(clean):
    redwood = ids(clean)["redwood-coast"]
    detail = login(clean, OWNER).get(f"{API_PREFIX}/map/deployments/{redwood}").json()
    by_mac = {row["mac"]: row for row in detail["listeners"]}
    assert len(by_mac) == 16
    first, second = by_mac["02:EE:0E:01:01:01"], by_mac["02:EE:0E:01:01:02"]
    assert (first["gps_lat"], first["gps_lon"]) == (41.2222, -124.012)
    assert (second["gps_lat"], second["gps_lon"]) == (None, None)
    alder = next(pod for pod in detail["pods"] if pod["name"] == "Pod 01 · Alder Creek")
    assert first["pod_id"] == alder["id"] and alder["listener_count"] == 8


@pytest.mark.integration
def test_a_scoped_operator_sees_one_deployment_and_a_404_for_the_other(clean):
    operator = login(clean, OPERATOR)
    rows = operator.get(f"{API_PREFIX}/map/deployments").json()
    assert [row["slug"] for row in rows] == ["redwood-coast"]
    other = ids(clean)["high-desert"]
    assert operator.get(f"{API_PREFIX}/map/deployments/{other}").status_code == 404
    assert operator.get(f"{API_PREFIX}/map/deployments/{uuid.uuid4()}").status_code == 404


@pytest.mark.integration
def test_the_map_needs_a_session(clean):
    anonymous = TestClient(clean, raise_server_exceptions=False)
    assert anonymous.get(f"{API_PREFIX}/map/deployments").status_code == 401


# --- 4. Constant query count ----------------------------------------------------


@pytest.fixture(scope="module")
def counting_db():
    with ephemeral_postgres() as url:
        from app.db import create_session_factory

        engine, factory = create_session_factory(url)
        with factory() as db:
            org = Organization(name="Count Org")
            db.add(org)
            db.flush()
            deployment = Deployment(organization_id=org.id, name="Count", slug="count")
            db.add(deployment)
            db.commit()
        yield engine, factory


def _grow(factory, pods: int, listeners_per_pod: int) -> uuid.UUID:
    """Make the Deployment hold exactly `pods` × `listeners_per_pod` devices."""
    with factory() as db:
        deployment = db.scalars(select(Deployment)).one()
        db.execute(delete(Listener))
        db.execute(delete(Aggregator))
        db.execute(delete(Pod))
        for p in range(pods):
            pod = Pod(deployment_id=deployment.id, name=f"pod-{p:03d}")
            db.add(pod)
            db.flush()
            aggregator = Aggregator(pod_id=pod.id, aggregator_uuid=f"count-{p:03d}")
            db.add(aggregator)
            db.flush()
            for n in range(listeners_per_pod):
                db.add(
                    Listener(
                        mac=f"02:CC:00:00:{p:02X}:{n:02X}",
                        name=f"l-{p:03d}-{n:03d}",
                        aggregator_id=aggregator.id,
                        deployment_id=deployment.id,
                    )
                )
        db.commit()
        return deployment.id


def _statements(engine, factory, call) -> int:
    seen: list[str] = []

    def count(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        with factory() as db:
            call(db)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(seen)


@pytest.mark.integration
def test_the_query_count_does_not_grow_with_the_fleet(counting_db):
    """Phase-6 fixed choice 5. Measured at 1 Listener and at 1,000 (20 Pods ×
    50, spec 14.2's v1 scale for one Deployment): the same number of queries,
    for both drill levels."""
    engine, factory = counting_db
    counts = []
    for pods, per_pod in ((1, 1), (20, 50)):
        deployment_id = _grow(factory, pods, per_pod)
        layer = _statements(engine, factory, lambda db: deployments_layer(db, "all"))
        detail = _statements(
            engine,
            factory,
            lambda db, dep=deployment_id: deployment_detail(db, db.get(Deployment, dep)),
        )
        counts.append((layer, detail))
    assert counts[0] == counts[1], f"query count grew with the fleet: {counts}"
