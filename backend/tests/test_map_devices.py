"""E6.5: the drawer's two reads (spec 6.5, 9.2, 9.3, 13; phase-6 E6.5).

- `GET /map/devices/{entity_type}/{id}`: the Status tab, for a Listener,
  Aggregator, Pod or Deployment. Its status is the marker's, from the same
  readers and rollup, so the drawer and the marker cannot disagree (D60).
  A Listener carries spec 6.5 liveness as reported, and the wake grace with
  the level it is inherited from.
- `GET /provisioning/records?target_type=&target_id=`: the Card tab, a
  filtered read of existing rows in the D7 envelope, newest first, scoped by
  VIEW_PROVISIONING.

Both answer out-of-scope the way the hierarchy does (D35): a device is a
404, and records outside the caller's scope are absent.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.config.overrides import put_overrides
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AggregatorStatus,
    Deployment,
    DeviceProvisioningRecord,
    DeviceState,
    EntityOverride,
    InventoryAlert,
    Listener,
    Pod,
    ProvisioningBundle,
    RoleAssignment,
    User,
)
from app.seed import seed_demo_hierarchy
from app.settings import Settings

pytestmark = [pytest.mark.integration]

OWNER = "drawer-owner@example.com"
OPERATOR = "drawer-operator@example.com"  # Redwood Coast only
MAC = "02:EE:0E:01:01:01"  # alder-creek-01, Pod 01, Redwood Coast
GRACE = "listener.wake_grace_seconds"


@pytest.fixture(scope="module")
def app():
    with ephemeral_postgres() as url:
        application = create_app(
            Settings(
                database_url=url,
                session_secret="e6-drawer",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
            )
        )
        with application.state.session_factory() as db:
            seed_demo_hierarchy(db)
            db.flush()
            redwood = db.scalars(select(Deployment).where(Deployment.slug == "redwood-coast")).one()
            users = []
            for email, role, scope in (
                (OWNER, "owner", None),
                (OPERATOR, "deployment_operator", redwood.id),
            ):
                user = User(email=email, password_hash=hash_password(PASSWORD))
                user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
                users.append(user)
            db.add_all(users)
            db.commit()
        yield application


@pytest.fixture
def clean(app):
    yield app
    with app.state.session_factory() as db:
        for model in (
            InventoryAlert,
            AggregatorStatus,
            DeviceState,
            DeviceProvisioningRecord,
            ProvisioningBundle,
            EntityOverride,
        ):
            db.execute(delete(model))
        db.commit()


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _row(app, model, **where):
    with app.state.session_factory() as db:
        return db.scalars(select(model).filter_by(**where)).one()


def device(client: TestClient, kind: str, key: object):
    return client.get(f"{API_PREFIX}/map/devices/{kind}/{key}")


# --- Listener ----------------------------------------------------------------


def test_a_silent_listener_says_so_rather_than_guessing(clean):
    body = device(login(clean, OWNER), "listener", MAC).json()
    assert body["entity_type"] == "listener" and body["id"] == MAC
    assert body["status"] == "unknown"
    assert body["pod_name"] == "Pod 01 · Alder Creek"
    assert body["deployment_name"] == "Redwood Coast"
    liveness = body["liveness"]
    assert {k: liveness[k] for k in ("state", "last_audio_at", "expected_wake_at")} == {
        "state": None,
        "last_audio_at": None,
        "expected_wake_at": None,
    }
    # Nobody overrode the grace, so it is the catalog default.
    assert liveness["grace"]["source"] == "default"
    assert isinstance(liveness["grace"]["seconds"], int)
    assert body["aggregator"]["aggregator_uuid"] == "demo-agg-rc-01"
    assert body["aggregator"]["online"] is None and body["alerts"] == []


def test_a_sleeping_listener_shows_its_liveness_and_the_inherited_grace(clean):
    listener = _row(clean, Listener, mac=MAC)
    now = datetime.now(UTC).replace(microsecond=0)
    with clean.state.session_factory() as db:
        db.add(
            DeviceState(
                entity_type="listener",
                entity_id=MAC,
                deployment_id=listener.deployment_id,
                reported_at=now,
                checksum="sha256:" + "0" * 64,
                config={},
                liveness_state="sleeping",
                last_audio_at=now - timedelta(minutes=7),
                expected_wake_at=now + timedelta(minutes=5),
                liveness_changed_at=now - timedelta(minutes=6),
            )
        )
        put_overrides(
            db, clean.state.secret_store, "deployment", str(listener.deployment_id), {GRACE: 75}
        )
        db.commit()

    body = device(login(clean, OWNER), "listener", MAC.lower().replace(":", "-")).json()
    assert body["status"] == "sleeping"
    liveness = body["liveness"]
    assert liveness["state"] == "sleeping"
    assert datetime.fromisoformat(liveness["expected_wake_at"]) == now + timedelta(minutes=5)
    assert datetime.fromisoformat(liveness["last_audio_at"]) == now - timedelta(minutes=7)
    assert liveness["grace"] == {
        "seconds": 75,
        "source": "deployment",
        "source_entity_id": str(listener.deployment_id),
    }


def test_a_listeners_open_alerts_come_with_their_detail(clean):
    listener = _row(clean, Listener, mac=MAC)
    with clean.state.session_factory() as db:
        db.add_all(
            [
                InventoryAlert(
                    alert_type="duplicate_identity",
                    entity_type="listener",
                    entity_key=MAC,
                    deployment_id=listener.deployment_id,
                    detail={"reported_by": "demo-agg-rc-02"},
                ),
                InventoryAlert(
                    alert_type="duplicate_identity",
                    entity_type="listener",
                    entity_key="02:EE:0E:01:01:02",  # a neighbour's: not this drawer's
                    deployment_id=listener.deployment_id,
                ),
            ]
        )
        db.commit()
    body = device(login(clean, OWNER), "listener", MAC).json()
    assert body["status"] == "alerting"
    assert [(a["alert_type"], a["detail"]) for a in body["alerts"]] == [
        ("duplicate_identity", {"reported_by": "demo-agg-rc-02"})
    ]


# --- Aggregator, Pod, Deployment -----------------------------------------------


def test_an_offline_aggregator_says_since_when(clean):
    aggregator = _row(clean, Aggregator, aggregator_uuid="demo-agg-rc-02")
    pod = _row(clean, Pod, id=aggregator.pod_id)
    changed = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)
    with clean.state.session_factory() as db:
        db.add(
            AggregatorStatus(
                aggregator_id=aggregator.id,
                deployment_id=pod.deployment_id,
                online=False,
                declared_at=changed,
                changed_at=changed,
            )
        )
        db.commit()
    body = device(login(clean, OWNER), "aggregator", aggregator.id).json()
    assert body["entity_type"] == "aggregator" and body["status"] == "offline"
    assert body["aggregator"]["online"] is False
    assert datetime.fromisoformat(body["aggregator"]["online_changed_at"]) == changed
    assert body["revision_state"] is None
    assert body["liveness"] is None and body["counts"] is None


def test_pod_and_deployment_drawers_agree_with_their_markers(clean):
    """The drawer reads the marker's own rollup, so a status and its counts
    are the same in both places for every Pod and the Deployment."""
    redwood = _row(clean, Deployment, slug="redwood-coast")
    aggregator = _row(clean, Aggregator, aggregator_uuid="demo-agg-rc-02")
    with clean.state.session_factory() as db:
        now = datetime.now(UTC)
        db.add(
            AggregatorStatus(
                aggregator_id=aggregator.id,
                deployment_id=redwood.id,
                online=False,
                declared_at=now,
                changed_at=now,
            )
        )
        # Names the Deployment and no device in inventory: unattached (D191).
        db.add(
            InventoryAlert(
                alert_type="provisioning_required",
                entity_type="aggregator",
                entity_key="never-entered",
                deployment_id=redwood.id,
            )
        )
        db.commit()
    owner = login(clean, OWNER)
    layer = owner.get(f"{API_PREFIX}/map/deployments/{redwood.id}").json()

    for pod in layer["pods"]:
        drawer = device(owner, "pod", pod["id"]).json()
        assert (drawer["status"], drawer["counts"]) == (pod["status"], pod["counts"])
        assert drawer["listener_count"] == pod["listener_count"]
        assert drawer["aggregator"]["status"] == pod["aggregator_status"]
        assert (drawer["gps_lat"], drawer["gps_lon"]) == (pod["gps_lat"], pod["gps_lon"])

    drawer = device(owner, "deployment", redwood.id).json()
    marker = layer["deployment"]
    assert drawer["status"] == marker["status"] == "offline"
    assert drawer["counts"] == marker["counts"]
    assert (drawer["pod_count"], drawer["listener_count"]) == (3, 16)
    assert drawer["services_status"] == "unconfigured"
    assert [a["entity_key"] for a in drawer["alerts"]] == ["never-entered"]

    for listener in layer["listeners"]:
        assert device(owner, "listener", listener["mac"]).json()["status"] == listener["status"]


# --- Scope and shape ---------------------------------------------------------------


def test_out_of_scope_and_missing_are_the_same_404(clean):
    operator = login(clean, OPERATOR)
    high_desert = _row(clean, Deployment, slug="high-desert")
    redwood = _row(clean, Deployment, slug="redwood-coast")
    assert device(operator, "deployment", redwood.id).status_code == 200
    assert device(operator, "deployment", high_desert.id).status_code == 404
    with clean.state.session_factory() as db:
        far_mac = db.scalars(
            select(Listener.mac).where(Listener.deployment_id == high_desert.id)
        ).first()
    assert device(operator, "listener", far_mac).status_code == 404
    for kind, key in (
        ("listener", "AA:BB:CC:DD:EE:FF"),
        ("listener", "not-a-mac"),
        ("pod", uuid.uuid4()),
        ("aggregator", "not-a-uuid"),
    ):
        assert device(operator, kind, key).status_code == 404, (kind, key)
    assert device(operator, "organization", redwood.id).status_code == 422


def test_the_drawer_needs_a_session(clean):
    anonymous = TestClient(clean, raise_server_exceptions=False)
    assert device(anonymous, "listener", MAC).status_code == 401


# --- The Card tab's filtered read ---------------------------------------------------


def _bundle_with_records(app, deployment_slug: str, targets: list[tuple[str, str]], at: datetime):
    deployment = _row(app, Deployment, slug=deployment_slug)
    with app.state.session_factory() as db:
        pod = db.scalars(select(Pod).where(Pod.deployment_id == deployment.id)).first()
        assert pod is not None
        aggregator = db.scalars(select(Aggregator).where(Aggregator.pod_id == pod.id)).one()
        bundle = ProvisioningBundle(
            deployment_id=deployment.id,
            scope_type="pod",
            scope_id=pod.id,
            plan={},
            template_version=1,
            firmware_key_version=1,
            archive_checksum="sha256:" + "a" * 64,
            generated_at=at,
        )
        db.add(bundle)
        db.flush()
        for target_type, target_id in targets:
            db.add(
                DeviceProvisioningRecord(
                    bundle_id=bundle.id,
                    deployment_id=deployment.id,
                    pod_id=pod.id,
                    aggregator_uuid=aggregator.aggregator_uuid,
                    target_type=target_type,
                    target_id=target_id,
                    file_kind="listener_file" if target_type == "listener" else "pod_file",
                    file_path=f"cards/{target_id}.bin",
                    status="generated",
                    status_history=[],
                    created_at=at,
                )
            )
        db.commit()
        return bundle.id


def records(client: TestClient, target_type: str, target_id: str):
    return client.get(
        f"{API_PREFIX}/provisioning/records",
        params={"target_type": target_type, "target_id": target_id},
    )


def test_a_devices_records_come_newest_first_in_the_envelope(clean):
    first = datetime(2026, 11, 1, tzinfo=UTC)
    older = _bundle_with_records(clean, "redwood-coast", [("listener", MAC)], first)
    newer = _bundle_with_records(
        clean,
        "redwood-coast",
        [("listener", MAC), ("listener", "02:EE:0E:01:01:02")],
        first + timedelta(days=3),
    )
    owner = login(clean, OWNER)
    body = records(owner, "listener", MAC.lower()).json()
    assert body["total"] == 2 and body["limit"] == 50 and body["offset"] == 0
    with clean.state.session_factory() as db:
        bundle_of = {row.id: row.bundle_id for row in db.scalars(select(DeviceProvisioningRecord))}
    assert [bundle_of[uuid.UUID(item["id"])] for item in body["items"]] == [newer, older]
    assert all(item["target_id"] == MAC for item in body["items"])
    assert records(owner, "aggregator", str(uuid.uuid4())).json()["total"] == 0


def test_records_outside_scope_are_absent_not_forbidden(clean):
    with clean.state.session_factory() as db:
        far = db.scalars(
            select(Listener.mac)
            .join(Deployment, Deployment.id == Listener.deployment_id)
            .where(Deployment.slug == "high-desert")
        ).first()
    assert far is not None
    _bundle_with_records(clean, "high-desert", [("listener", far)], datetime.now(UTC))
    assert records(login(clean, OWNER), "listener", far).json()["total"] == 1
    response = records(login(clean, OPERATOR), "listener", far)
    assert response.status_code == 200 and response.json()["total"] == 0


def test_the_filter_is_required_and_closed(clean):
    owner = login(clean, OWNER)
    assert owner.get(f"{API_PREFIX}/provisioning/records").status_code == 422
    assert records(owner, "pod", str(uuid.uuid4())).status_code == 422
    assert records(owner, "listener", "not-a-mac").status_code == 422
