"""E6.9: a Listener behind an offline Aggregator is `unreachable` (D202; SPEC-9-02).

The owner's rule, 2026-11-14: a Listener under an offline Aggregator should
have a status that says so. Its liveness is whatever the Aggregator last
reported (E3.9), and an offline Aggregator reports nothing, so the stored
liveness is history.

- **Pure:** whatever a Listener last was, an Aggregator verdict of `offline`
  makes it `unreachable`; `online` and "never spoke" change nothing; an open
  alert still wins.
- **One derivation everywhere (D60):** the inventory API, the map read and the
  drawer all say `unreachable` for the same Listener, and the Pod still reads
  `offline`, its Aggregator's own status, which ranks above.
"""

from datetime import UTC, datetime

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.controlplane.device_status import (
    SEVERITY,
    DeviceStatus,
    behind_aggregator,
    with_alerts,
)
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AggregatorStatus,
    DeviceState,
    InventoryAlert,
    Listener,
    Pod,
    RoleAssignment,
    User,
)
from app.seed import seed_demo_hierarchy
from app.settings import Settings

S = DeviceStatus

# --- Pure -------------------------------------------------------------------------


@pytest.mark.parametrize("status", list(DeviceStatus))
def test_an_offline_aggregator_makes_any_listener_unreachable(status):
    assert behind_aggregator(status, False) is S.UNREACHABLE


@pytest.mark.parametrize("status", list(DeviceStatus))
@pytest.mark.parametrize("online", [True, None])
def test_an_online_or_silent_aggregator_changes_nothing(status, online):
    assert behind_aggregator(status, online) is status


def test_an_open_alert_still_wins_over_unreachable():
    assert with_alerts(behind_aggregator(S.HEALTHY, False), 1) is S.ALERTING


def test_unreachable_ranks_just_below_offline():
    """The Aggregator that cuts a Listener off is offline in the same Pod, and
    that is what an operator must go and fix."""
    assert SEVERITY.index(S.UNREACHABLE) == SEVERITY.index(S.OFFLINE) + 1


# --- Through every surface ------------------------------------------------------

OWNER = "unreachable-owner@example.com"
AGG = "demo-agg-rc-01"  # Pod 01 · Alder Creek


@pytest.fixture(scope="module")
def app():
    with ephemeral_postgres() as url:
        application = create_app(
            Settings(
                database_url=url,
                session_secret="e6-unreachable",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
            )
        )
        with application.state.session_factory() as db:
            seed_demo_hierarchy(db)
            owner = User(email=OWNER, password_hash=hash_password(PASSWORD))
            owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
            db.add(owner)
            db.commit()
        yield application


@pytest.fixture
def owner(app):
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200, response.text
    yield client
    with app.state.session_factory() as db:
        for model in (AggregatorStatus, DeviceState, InventoryAlert):
            db.execute(delete(model))
        db.commit()


def _alder(app) -> tuple[Aggregator, Pod, list[Listener]]:
    with app.state.session_factory() as db:
        aggregator = db.scalars(select(Aggregator).where(Aggregator.aggregator_uuid == AGG)).one()
        pod = db.get_one(Pod, aggregator.pod_id)
        listeners = list(
            db.scalars(
                select(Listener)
                .where(Listener.aggregator_id == aggregator.id)
                .order_by(Listener.mac)
            )
        )
        return aggregator, pod, listeners


def _verdict(app, aggregator: Aggregator, pod: Pod, *, online: bool) -> None:
    now = datetime.now(UTC)
    with app.state.session_factory() as db:
        db.execute(delete(AggregatorStatus).where(AggregatorStatus.aggregator_id == aggregator.id))
        db.add(
            AggregatorStatus(
                aggregator_id=aggregator.id,
                deployment_id=pod.deployment_id,
                online=online,
                declared_at=now,
                changed_at=now,
            )
        )
        db.commit()


def _streaming(app, listener: Listener) -> None:
    with app.state.session_factory() as db:
        db.add(
            DeviceState(
                entity_type="listener",
                entity_id=listener.mac,
                deployment_id=listener.deployment_id,
                reported_at=datetime.now(UTC),
                checksum="sha256:" + "0" * 64,
                config={},
                liveness_state="streaming",
                liveness_changed_at=datetime.now(UTC),
            )
        )
        db.commit()


@pytest.mark.integration
def test_a_streaming_listener_goes_unreachable_with_its_aggregator(app, owner):
    aggregator, pod, listeners = _alder(app)
    streaming, silent = listeners[0], listeners[1]
    _streaming(app, streaming)
    _verdict(app, aggregator, pod, online=True)
    assert owner.get(f"{API_PREFIX}/listeners/{streaming.mac}").json()["status"] == "healthy"

    _verdict(app, aggregator, pod, online=False)
    # Inventory, map and drawer: one derivation, one answer (D60).
    assert owner.get(f"{API_PREFIX}/listeners/{streaming.mac}").json()["status"] == "unreachable"
    detail = owner.get(f"{API_PREFIX}/map/deployments/{pod.deployment_id}").json()
    by_mac = {row["mac"]: row["status"] for row in detail["listeners"]}
    assert {by_mac[row.mac] for row in listeners} == {"unreachable"}, "never-heard ones too"
    drawer = owner.get(f"{API_PREFIX}/map/devices/listener/{streaming.mac}").json()
    assert drawer["status"] == "unreachable"
    assert drawer["liveness"]["state"] == "streaming", "the history is still shown, as history"
    assert drawer["aggregator"]["online"] is False

    # The Pod reads its Aggregator's own `offline`, which outranks, and its
    # counts say how many Listeners are cut off.
    alder = next(row for row in detail["pods"] if row["id"] == str(pod.id))
    assert alder["status"] == "offline"
    assert alder["counts"]["offline"] == 1
    assert alder["counts"]["unreachable"] == len(listeners)
    assert silent.mac in by_mac


@pytest.mark.integration
def test_an_alert_still_wins_and_other_pods_are_untouched(app, owner):
    aggregator, pod, listeners = _alder(app)
    _verdict(app, aggregator, pod, online=False)
    with app.state.session_factory() as db:
        db.add(
            InventoryAlert(
                alert_type="duplicate_identity",
                entity_type="listener",
                entity_key=listeners[0].mac,
                deployment_id=pod.deployment_id,
            )
        )
        db.commit()
    detail = owner.get(f"{API_PREFIX}/map/deployments/{pod.deployment_id}").json()
    by_mac = {row["mac"]: row["status"] for row in detail["listeners"]}
    assert by_mac[listeners[0].mac] == "alerting"
    others = [row for row in detail["listeners"] if row["pod_id"] != str(pod.id)]
    assert others and {row["status"] for row in others} == {"unknown"}


@pytest.mark.integration
def test_an_aggregator_coming_back_restores_the_reported_liveness(app, owner):
    aggregator, pod, listeners = _alder(app)
    _streaming(app, listeners[0])
    _verdict(app, aggregator, pod, online=False)
    assert owner.get(f"{API_PREFIX}/listeners/{listeners[0].mac}").json()["status"] == "unreachable"
    _verdict(app, aggregator, pod, online=True)
    assert owner.get(f"{API_PREFIX}/listeners/{listeners[0].mac}").json()["status"] == "healthy"
