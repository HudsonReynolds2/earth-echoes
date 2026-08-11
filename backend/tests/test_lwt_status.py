"""Gate 46: LWT status handling (task E3.8; spec 9.3, 7.2, 7.3, 6.4).

The phase document's acceptance is one sentence — "dropping a mock device's
connection without a clean disconnect flips it offline via LWT under test" —
and `test_a_device_that_dies_without_saying_goodbye_flips_offline` is it,
against a real Mosquitto, with a real will registered by a real client that is
then SIGKILLed. Nothing simulates the will: the broker composes and publishes
it, which is the only way to prove the platform is reading the signal spec 9.3
makes authoritative rather than something a test handed it.

Everything above that drives `ReportedConsumer.consume` directly, and the one
worth reading twice is
`test_an_lwt_whose_timestamp_predates_the_last_heartbeat_still_wins`. A device
composes its will when it CONNECTS, so the broker holds those bytes — with
that connect-time `at` — until the session dies. Every `online` published
afterwards carries a later timestamp. Ordering status by the payload clock the
way spec 7.4 orders reports would therefore reject the LWT as stale and leave
a dead device reading online forever, which is the exact failure spec 9.3
exists to prevent.
"""

import asyncio
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    docker_cli,
    docker_env,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from sqlalchemy import delete, select, update

from app.contracts.mqtt import StatusMessage, encode, status_topic
from app.controlplane.broker import (
    InboundMessage,
    MqttClientManager,
    load_broker_coordinates,
)
from app.controlplane.consumer import ReportedConsumer, ReportOutcome
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.inventory.identity import ALERT_PROVISIONING_REQUIRED
from app.models import (
    Aggregator,
    AggregatorStatus,
    Deployment,
    DeploymentService,
    DeviceState,
    InventoryAlert,
    Pod,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

RC = "redwood-coast"
HD = "high-desert"
AGG = "demo-agg-rc-01"
#: An aggregator_uuid in no inventory row — spec 4.3 item 3's case.
GHOST = "demo-agg-rc-ghost"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory


@pytest.fixture
def statuses(database):
    yield database
    with database() as db:
        db.execute(delete(AggregatorStatus))
        db.execute(delete(InventoryAlert))
        db.commit()


@pytest.fixture
def consumer(statuses) -> ReportedConsumer:
    return ReportedConsumer(statuses)


def deployment_id_of(factory, slug: str = RC) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def aggregator_id_of(factory, aggregator_uuid: str = AGG) -> uuid.UUID:
    with factory() as db:
        return db.scalars(
            select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()


def status_row(factory, aggregator_uuid: str = AGG) -> AggregatorStatus | None:
    with factory() as db:
        return db.scalars(
            select(AggregatorStatus).where(
                AggregatorStatus.aggregator_id == aggregator_id_of(factory, aggregator_uuid)
            )
        ).first()


def inbound(factory, payload: bytes, *, agg: str = AGG, slug: str = RC) -> InboundMessage:
    """One delivered status message. Retained on the wire (spec 7.2), which is
    what makes a reconnecting platform learn the fleet's liveness unprompted."""
    return InboundMessage(
        deployment_id=deployment_id_of(factory, slug),
        deployment_slug=slug,
        topic=status_topic(slug, agg),
        payload=payload,
        qos=1,
        retain=True,
    )


def status(state: str, at: datetime | None = None) -> bytes:
    return encode(StatusMessage(state=state, at=at or datetime.now(UTC)))


# --- The verdict ------------------------------------------------------------


def test_a_first_online_message_creates_the_verdict(consumer, statuses):
    assert consumer.consume(inbound(statuses, status("online"))) is ReportOutcome.ONLINE

    row = status_row(statuses)
    assert row is not None
    assert row.online is True
    assert row.deployment_id == deployment_id_of(statuses)


def test_a_device_with_no_status_has_no_row_at_all(consumer, statuses):
    """`online` is a two-valued column on purpose: "never spoken" is a
    different question from "told us it is offline", and a NULL third state
    would let the map paint a device that has never existed as a dead one."""
    assert status_row(statuses) is None


def test_offline_flips_the_verdict_and_stamps_when_it_changed(consumer, statuses):
    consumer.consume(inbound(statuses, status("online")))
    first = status_row(statuses)
    assert first is not None
    online_since = first.changed_at

    assert consumer.consume(inbound(statuses, status("offline"))) is ReportOutcome.OFFLINE

    row = status_row(statuses)
    assert row is not None
    assert row.online is False
    assert row.changed_at > online_since, "changed_at is what 'offline since' reads"


def test_a_retained_replay_of_the_same_verdict_moves_nothing(consumer, statuses):
    """The broker replays the retained status to the platform on EVERY
    reconnect. If that rewrote `changed_at`, a platform restart would reset
    the whole fleet's "offline since" to the moment the platform came back —
    telling an operator the outage started when their own service did.
    """
    consumer.consume(inbound(statuses, status("offline")))
    first = status_row(statuses)
    assert first is not None
    changed_at = first.changed_at

    assert consumer.consume(inbound(statuses, status("offline"))) is ReportOutcome.OFFLINE

    row = status_row(statuses)
    assert row is not None
    assert row.changed_at == changed_at, "a replay is not a new outage"


def test_an_lwt_whose_timestamp_predates_the_last_heartbeat_still_wins(consumer, statuses):
    """THE ordering property (models.AggregatorStatus).

    A device registers its will at CONNECT time, so the broker is holding an
    `offline` payload stamped when the device came UP. Every `online` it
    publishes afterwards is newer. Applying spec 7.4's staleness rule here —
    correct for reports, wrong for this — would drop the will and leave a dead
    device reading online for as long as the row survives.
    """
    connected_at = datetime.now(UTC) - timedelta(hours=3)
    will = status("offline", at=connected_at)

    consumer.consume(inbound(statuses, status("online", at=connected_at)))
    # Three hours of heartbeats, each newer than the will the broker holds.
    consumer.consume(inbound(statuses, status("online", at=datetime.now(UTC))))
    assert status_row(statuses).online is True

    # The device dies. The broker publishes the bytes it has held all along.
    assert consumer.consume(inbound(statuses, will)) is ReportOutcome.OFFLINE

    row = status_row(statuses)
    assert row is not None
    assert row.online is False, (
        "the LWT was rejected as stale — a dead device now reads online forever"
    )
    assert row.declared_at == connected_at, "the device's own clock is stored, just not obeyed"


# --- The boundary -----------------------------------------------------------


def test_a_status_message_never_touches_reported_state(consumer, statuses):
    """Reachability and configuration are different questions. A device that
    is online has said nothing about what config it is running."""
    consumer.consume(inbound(statuses, status("online")))
    with statuses() as db:
        assert db.scalars(select(DeviceState)).first() is None


def test_status_from_an_unprovisioned_aggregator_raises_the_alert(consumer, statuses):
    """Spec 4.3 item 3: a device nobody entered in inventory is surfaced, not
    silently dropped — and writes no verdict, because there is no row to hang
    it on."""
    assert consumer.consume(inbound(statuses, status("online"), agg=GHOST)) is (
        ReportOutcome.UNPROVISIONED
    )
    with statuses() as db:
        alerts = db.scalars(
            select(InventoryAlert).where(InventoryAlert.alert_type == ALERT_PROVISIONING_REQUIRED)
        ).all()
        assert len(alerts) == 1
        assert db.scalars(select(AggregatorStatus)).first() is None


def test_status_arriving_on_another_deployments_connection_is_dropped(consumer, statuses):
    """The identity is the topic's, and the topic was authenticated by the ACL
    of the broker this connection dialled. A redwood-coast device appearing on
    the high-desert connection is not a device the platform may believe."""
    message = inbound(statuses, status("online"), slug=HD)
    # The topic still names the RC aggregator; only the connection differs.
    message = InboundMessage(
        deployment_id=deployment_id_of(statuses, HD),
        deployment_slug=HD,
        topic=status_topic(RC, AGG),
        payload=status("online"),
        qos=1,
        retain=True,
    )
    assert consumer.consume(message) is ReportOutcome.MISROUTED
    assert status_row(statuses) is None


def test_an_undecodable_status_is_malformed_and_writes_nothing(consumer, statuses):
    assert consumer.consume(inbound(statuses, b'{"schema_version":1}')) is ReportOutcome.MALFORMED
    assert status_row(statuses) is None


def test_a_state_outside_the_spec_vocabulary_is_refused(consumer, statuses):
    """spec 7.3 names exactly two states. `degraded` is a status model concept
    (spec 9.3) the platform DERIVES; a device may not declare it here."""
    payload = b'{"schema_version":1,"state":"degraded","at":"2026-08-10T00:00:00Z"}'
    assert consumer.consume(inbound(statuses, payload)) is ReportOutcome.MALFORMED
    assert status_row(statuses) is None


def test_the_verdict_dies_with_its_device(consumer, statuses):
    """Current state, not evidence: a decommissioned Aggregator must not leave
    a verdict behind for a future device to inherit. The FK cascades, which is
    why this needs no cleanup helper of its own (unlike `device_state`, whose
    two target kinds leave it un-FK'd)."""
    with statuses() as db:
        # Its own pod: `aggregator.pod_id` is unique, one Aggregator per Pod.
        pod = Pod(deployment_id=deployment_id_of(statuses), name="qa-doomed-pod")
        db.add(pod)
        db.flush()
        doomed = Aggregator(pod_id=pod.id, aggregator_uuid="demo-agg-rc-doomed")
        db.add(doomed)
        db.commit()
        doomed_id, pod_id = doomed.id, pod.id

    consumer.consume(inbound(statuses, status("online"), agg="demo-agg-rc-doomed"))
    with statuses() as db:
        assert (
            db.scalars(
                select(AggregatorStatus).where(AggregatorStatus.aggregator_id == doomed_id)
            ).first()
            is not None
        )
        db.delete(db.get(Aggregator, doomed_id))
        db.commit()
        assert (
            db.scalars(
                select(AggregatorStatus).where(AggregatorStatus.aggregator_id == doomed_id)
            ).first()
            is None
        )
        db.delete(db.get(Pod, pod_id))
        db.commit()


# --- ACCEPTANCE: a real device, a real will, a real unclean death ------------


def _provision(url: str, kek: str, out_dir) -> None:
    import os
    import sys

    from conftest import REPO_ROOT

    env = {
        **os.environ,
        "DATABASE_URL": url,
        "EOE_KEK": kek,
        "EOE_SESSION_SECRET": "gate46-secret",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out_dir)],
        cwd=REPO_ROOT / "backend",
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed: {result.stdout}\n{result.stderr}"


@pytest.fixture
def live_stack(tmp_path_factory):
    out = tmp_path_factory.mktemp("lwt-certs")
    port = free_port()
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with factory() as db:
            db.execute(update(DeploymentService).values(port=port))
            db.commit()
        with ephemeral_broker(out, host_port=port) as broker:
            yield broker, factory, SecretStore(factory, kek), load_manifest(out)


def device_credentials(manifest, aggregator_uuid: str = AGG) -> tuple[str, str]:
    username = device_username(aggregator_uuid)
    return username, next(a["password"] for a in manifest["accounts"] if a["username"] == username)


#: The mock device's MQTT client id, so the kill below hits exactly it.
PROBE_ID = "eoe-lwt-probe"


def start_device_holding_a_will(broker, manifest, will: bytes) -> None:
    """A mock Aggregator that connects, registers its will, and then just sits
    there — `docker exec -d`, so it outlives this call.

    It subscribes to its own desired topic, which is what a real Aggregator
    does and what the spec 7.1 ACL grants it. The point is only that it holds
    an open session the broker can notice the death of.
    """
    username, password = device_credentials(manifest)
    started = subprocess.run(
        [
            docker_cli(),
            "exec",
            "-d",
            broker.name,
            "mosquitto_sub",
            "-h",
            "localhost",
            "-p",
            "8883",
            "--cafile",
            "/mosquitto/dev/ca.crt",
            "-u",
            username,
            "-P",
            password,
            "-i",
            PROBE_ID,
            "-t",
            f"eoe/{RC}/agg/{AGG}/desired",
            "--will-topic",
            status_topic(RC, AGG),
            "--will-payload",
            will.decode("utf-8"),
            "--will-qos",
            "1",
            "--will-retain",
        ],
        capture_output=True,
        text=True,
        env=docker_env(),
        timeout=30,
    )
    assert started.returncode == 0, f"could not start the mock device: {started.stderr}"


def kill_the_device_uncleanly(broker) -> None:
    """SIGKILL. No DISCONNECT packet reaches the broker, which is the entire
    difference between this and a device shutting down politely: MQTT publishes
    the will on any close that was not a DISCONNECT.
    """
    killed = subprocess.run(
        [docker_cli(), "exec", broker.name, "pkill", "-9", "-f", PROBE_ID],
        capture_output=True,
        text=True,
        env=docker_env(),
        timeout=30,
    )
    assert killed.returncode == 0, f"the mock device was not running: {killed.stderr}"


async def wait_for_verdict(factory, *, online: bool, timeout: float = 20.0) -> AggregatorStatus:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = status_row(factory)
        if row is not None and row.online is online:
            return row
        await asyncio.sleep(0.2)
    raise AssertionError(f"the platform never recorded online={online}: {status_row(factory)}")


async def test_a_device_that_dies_without_saying_goodbye_flips_offline(live_stack):
    """ACCEPTANCE (phase doc E3.8).

    A real client registers a real will with a real Mosquitto and is then
    SIGKILLed. The broker — not this test — composes and publishes the
    `offline` message, and the platform's own consumer picks it up off the
    subscription it already had. That is spec 9.3's authoritative liveness
    signal, end to end.
    """
    broker, factory, store, manifest = live_stack
    consumer = ReportedConsumer(factory)
    manager = MqttClientManager(lambda: load_broker_coordinates(factory, store))
    manager.subscribe(consumer.filters, consumer.handle)

    async with manager:
        await manager.wait_connected(deployment_id_of(factory))

        # The will is stamped NOW, at connect time — which is exactly why it
        # will be older than the heartbeat that follows it.
        will = status("offline", at=datetime.now(UTC))
        start_device_holding_a_will(broker, manifest, will)
        await asyncio.sleep(1.0)

        await manager.publish(
            deployment_id_of(factory),
            status_topic(RC, AGG),
            status("online", at=datetime.now(UTC)),
            retain=True,
        )
        await wait_for_verdict(factory, online=True)

        kill_the_device_uncleanly(broker)
        row = await wait_for_verdict(factory, online=False)

    assert row.declared_at < row.changed_at, (
        "the will's own timestamp is older than the moment the platform acted "
        "on it, which is the whole reason it is not an ordering key"
    )
