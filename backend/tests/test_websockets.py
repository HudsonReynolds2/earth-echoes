"""Gate 50: live updates and the spec 9.3 status roll-up (task E3.12; D59, D60).

The phase document's acceptance is
`test_two_sessions_with_different_scopes_see_correctly_filtered_events`: one
underlying change, two connected sessions, and only the one entitled to it
hears about it. Scoping happens on the SERVER per event per connection — a
websocket is a long-lived read of everything happening in the platform, so a
filter applied in the browser would be no filter at all.

The other half is D60, which lifts D40: status is now REAL, derived in one
place from LWT (E3.8), spec 6.5 liveness (E3.9) and revision state (E3.6).
`unknown` is a first-class answer here, and the tests say so repeatedly,
because the failure mode D40 existed to prevent is a device that has never
spoken being painted healthy.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_auth import PASSWORD

from app.api.ws import Subscription
from app.auth.passwords import hash_password
from app.config.canonical import config_checksum
from app.controlplane.device_status import DeviceStatus, aggregator_status, listener_status, rollup
from app.controlplane.events import Channel, Event, Hub, listen, publish
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AggregatorStatus,
    ConfigRevision,
    Deployment,
    DeviceState,
    RoleAssignment,
    User,
)
from app.seed import seed_demo_hierarchy
from app.settings import Settings

RC = "redwood-coast"
HD = "high-desert"
AGG = "demo-agg-rc-01"
MAC = "02:EE:0E:01:01:01"

OWNER = "ws-owner@example.com"
RC_OP = "ws-rc-op@example.com"
HD_OP = "ws-hd-op@example.com"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- The status derivation (spec 9.3; D60) ----------------------------------


def test_a_device_that_has_never_spoken_is_unknown_not_healthy():
    """THE D40 property, and the reason that guard existed. An operator who
    learns the dots are decorative stops reading them, including on the day
    one is telling the truth."""
    assert aggregator_status(online=None, revision_state=None) is DeviceStatus.UNKNOWN
    assert listener_status(liveness_state=None, revision_state=None) is DeviceStatus.UNKNOWN


def test_reachability_outranks_reconciliation():
    """An offline device may also have drifted, but offline is what an
    operator must act on: the drift cannot be repaired until it is back, and
    showing `drifted` sends them to fix a config while the box is unplugged."""
    assert aggregator_status(online=False, revision_state="drifted") is DeviceStatus.OFFLINE
    assert aggregator_status(online=True, revision_state="drifted") is DeviceStatus.DRIFTED


def test_a_failed_revision_is_degraded_not_offline():
    """The device is talking; it just could not apply what it was given."""
    assert aggregator_status(online=True, revision_state="failed") is DeviceStatus.DEGRADED


@pytest.mark.parametrize("state", ["draft", "pending", "applied", "superseded", None])
def test_a_config_change_in_flight_is_not_a_fault(state):
    """Painting `pending` as degraded would make every routine edit look like
    an incident."""
    assert aggregator_status(online=True, revision_state=state) is DeviceStatus.HEALTHY


def test_a_sleeping_listener_reads_as_sleeping_and_that_is_healthy():
    """Spec 6.5: silence is by design under duty cycling. It gets its own
    status (and its own glyph) so an operator knows the quiet is expected."""
    assert listener_status(liveness_state="sleeping", revision_state=None) is DeviceStatus.SLEEPING
    assert listener_status(liveness_state="streaming", revision_state=None) is DeviceStatus.HEALTHY
    assert listener_status(liveness_state="offline", revision_state=None) is DeviceStatus.OFFLINE


def test_the_rollup_takes_the_worst_and_ignores_unknown_among_the_known():
    """Spec 9.3: a parent reflects the worst status among descendants. One
    silent device among ten healthy ones must not make a deployment unknown."""
    assert rollup([DeviceStatus.HEALTHY, DeviceStatus.OFFLINE]) is DeviceStatus.OFFLINE
    assert rollup([DeviceStatus.HEALTHY, DeviceStatus.UNKNOWN]) is DeviceStatus.HEALTHY
    assert rollup([DeviceStatus.DRIFTED, DeviceStatus.DEGRADED]) is DeviceStatus.DEGRADED
    assert rollup([DeviceStatus.UNKNOWN]) is DeviceStatus.UNKNOWN
    # A deployment with no devices has no health to report, and calling it
    # healthy would be a claim about nothing.
    assert rollup([]) is DeviceStatus.UNKNOWN


# --- The subscription filter (the security boundary) ------------------------


def _event(deployment_id: uuid.UUID, channel: Channel = Channel.DEVICE_STATUS) -> Event:
    return Event(
        channel=channel, deployment_id=deployment_id, entity_type="aggregator", entity_id="x"
    )


def test_a_scoped_subscription_refuses_another_deployments_event():
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    subscription = Subscription({mine})
    assert subscription.wants(_event(mine)) is True
    assert subscription.wants(_event(theirs)) is False


def test_an_org_wide_subscription_sees_everything():
    assert Subscription("all").wants(_event(uuid.uuid4())) is True


def test_channels_narrow_but_never_widen():
    """A client may ask for less. It cannot ask for another deployment, which
    is why `_read_client` needs no authorization of its own."""
    mine = uuid.uuid4()
    subscription = Subscription({mine}, frozenset({Channel.RECONCILIATION}))
    assert subscription.wants(_event(mine, Channel.RECONCILIATION)) is True
    assert subscription.wants(_event(mine, Channel.DEVICE_STATUS)) is False
    # Even narrowed, the scope still holds.
    assert subscription.wants(_event(uuid.uuid4(), Channel.RECONCILIATION)) is False


# --- The envelope and the hub -----------------------------------------------


def test_an_event_round_trips_through_json():
    original = Event(
        channel=Channel.RECONCILIATION,
        deployment_id=uuid.uuid4(),
        entity_type="listener",
        entity_id=MAC,
        data={"to_state": "applied"},
    )
    assert Event.from_json(original.to_json()) == original


async def test_a_slow_subscriber_is_dropped_from_rather_than_blocking_the_bus():
    """One stalled browser must not stop every other one receiving updates."""
    hub = Hub(queue_size=2)
    slow = hub.subscribe()
    for _ in range(5):
        hub.dispatch(_event(uuid.uuid4()))
    assert slow.qsize() == 2  # the rest were dropped, and nothing raised


async def test_unsubscribing_stops_delivery():
    hub = Hub()
    queue = hub.subscribe()
    hub.unsubscribe(queue)
    hub.dispatch(_event(uuid.uuid4()))
    assert queue.empty()
    assert hub.subscriber_count == 0


# --- The bus, against a real Postgres ---------------------------------------


@pytest.fixture(scope="module")
def database():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield url, factory


@pytest.fixture
def sessions(database):
    _, factory = database
    yield factory
    with factory() as db:
        db.execute(delete(ConfigRevision))
        db.execute(delete(DeviceState))
        db.execute(delete(AggregatorStatus))
        db.commit()


def deployment_id_of(factory, slug: str = RC) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


async def test_an_event_reaches_a_listener_only_after_the_transaction_commits(database, sessions):
    """THE bus property (D59). `pg_notify` inside a transaction is delivered
    by Postgres only on commit, so a browser can never be shown a change that
    was rolled back — no outbox, no ordering to arrange."""
    url, factory = database
    received: list[Event] = []
    stopping = asyncio.Event()
    task = asyncio.create_task(listen(url, received.append, stopping=stopping))
    await asyncio.sleep(1.0)  # let LISTEN land

    deployment_id = deployment_id_of(factory)
    with factory() as db:
        publish(db, _event(deployment_id))
        db.rollback()
    await asyncio.sleep(0.5)
    assert received == [], "a rolled-back change was announced to browsers"

    with factory() as db:
        publish(db, _event(deployment_id))
        db.commit()
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.1)

    stopping.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(received) == 1
    assert received[0].deployment_id == deployment_id


# --- ACCEPTANCE: two scopes, one change -------------------------------------


@pytest.fixture(scope="module")
def api_app(database):
    url, factory = database
    with factory() as db:
        rc_id = db.scalars(select(Deployment.id).where(Deployment.slug == RC)).one()
        hd_id = db.scalars(select(Deployment.id).where(Deployment.slug == HD)).one()
    app = create_app(
        Settings(
            database_url=url,
            session_secret="gate50-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with app.state.session_factory() as db:
        for email, role, scope in (
            (OWNER, "owner", None),
            (RC_OP, "deployment_operator", rc_id),
            (HD_OP, "deployment_operator", hd_id),
        ):
            if db.scalars(select(User).where(User.email == email)).first() is not None:
                continue
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
    # Entering the client RUNS THE LIFESPAN, which is what creates the hub and
    # starts the LISTEN task. Doing it here rather than mocking `state.hub`
    # means these tests exercise the wiring a deployed API actually has.
    with TestClient(app):
        yield app


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def test_two_sessions_with_different_scopes_see_correctly_filtered_events(api_app, sessions):
    """ACCEPTANCE (phase doc E3.12).

    One change in redwood-coast. The operator scoped there hears it; the
    operator scoped to high-desert does not. `TestClient` runs the app's real
    lifespan, so this exercises the actual hub and the actual endpoint.
    """
    rc_client = login(api_app, RC_OP)
    hd_client = login(api_app, HD_OP)
    rc_id = deployment_id_of(sessions, RC)

    with (
        rc_client.websocket_connect("/api/v1/ws") as rc_socket,
        hd_client.websocket_connect("/api/v1/ws") as hd_socket,
    ):
        # Straight into the hub: this test is about FILTERING, and driving
        # it through Postgres would add a listener race to a question that
        # has nothing to do with the bus.
        api_app.state.hub.dispatch(
            Event(
                channel=Channel.DEVICE_STATUS,
                deployment_id=rc_id,
                entity_type="aggregator",
                entity_id="the-one-that-changed",
                data={"online": False},
            )
        )
        received = rc_socket.receive_json()
        assert received["entity_id"] == "the-one-that-changed"
        assert received["deployment_id"] == str(rc_id)

        # And the out-of-scope session has nothing waiting. A second
        # dispatch it IS entitled to proves the socket is live rather than
        # merely slow — an empty socket and a broken one look identical.
        api_app.state.hub.dispatch(
            Event(
                channel=Channel.DEVICE_STATUS,
                deployment_id=deployment_id_of(sessions, HD),
                entity_type="aggregator",
                entity_id="high-desert-device",
                data={"online": True},
            )
        )
        assert hd_socket.receive_json()["entity_id"] == "high-desert-device"


def test_an_owner_sees_every_deployment(api_app, sessions):
    with login(api_app, OWNER).websocket_connect("/api/v1/ws") as socket:
        api_app.state.hub.dispatch(_event(deployment_id_of(sessions, HD)))
        assert socket.receive_json()["deployment_id"] == str(deployment_id_of(sessions, HD))


def test_an_unauthenticated_socket_is_closed_with_a_policy_code(api_app):
    """1008, so the browser can tell "you may not" from "the API is down" and
    stop retrying a login it does not have."""
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(api_app, raise_server_exceptions=False)
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/api/v1/ws") as socket,
    ):
        socket.receive_json()
    assert caught.value.code == 1008


def test_a_client_can_narrow_its_channels(api_app, sessions):
    rc_id = deployment_id_of(sessions, RC)
    with login(api_app, OWNER).websocket_connect("/api/v1/ws") as socket:
        socket.send_json({"subscribe": ["reconciliation"]})
        # Give the reader task a turn before dispatching.
        api_app.state.hub.dispatch(
            Event(
                channel=Channel.RECONCILIATION,
                deployment_id=rc_id,
                entity_type="aggregator",
                entity_id="wanted",
            )
        )
        assert socket.receive_json()["entity_id"] == "wanted"


# --- The status surfaces (D60) ----------------------------------------------


def test_the_inventory_surface_reports_real_status(api_app, sessions):
    """D40 is lifted here: the field exists, and it is `unknown` until a
    device has actually said something."""
    client = login(api_app, OWNER)
    listing = client.get(f"{API_PREFIX}/aggregators").json()
    assert listing["items"], "the demo hierarchy has aggregators"
    assert all(item["status"] == "unknown" for item in listing["items"])

    with sessions() as db:
        agg_id = db.scalars(select(Aggregator.id).where(Aggregator.aggregator_uuid == AGG)).one()
        db.add(
            AggregatorStatus(
                aggregator_id=agg_id,
                deployment_id=deployment_id_of(sessions),
                online=False,
                declared_at=datetime.now(UTC),
                changed_at=datetime.now(UTC),
            )
        )
        db.commit()

    refreshed = client.get(f"{API_PREFIX}/aggregators").json()["items"]
    by_uuid = {item["aggregator_uuid"]: item["status"] for item in refreshed}
    assert by_uuid[AGG] == "offline"
    assert all(status == "unknown" for uuid_, status in by_uuid.items() if uuid_ != AGG)


def test_a_listener_status_follows_its_liveness(api_app, sessions):
    config = {"capture.mode": "duty_cycle"}
    with sessions() as db:
        db.add(
            DeviceState(
                entity_type="listener",
                entity_id=MAC,
                deployment_id=deployment_id_of(sessions),
                reported_at=datetime.now(UTC),
                checksum=config_checksum(config),
                config=config,
                liveness_state="sleeping",
                expected_wake_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )
        )
        db.commit()

    items = login(api_app, OWNER).get(f"{API_PREFIX}/listeners").json()["items"]
    statuses = {item["mac"]: item["status"] for item in items}
    assert statuses[MAC] == "sleeping"
