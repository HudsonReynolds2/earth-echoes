"""SIM.1 acceptance: a mock Aggregator against a real broker and a real platform.

The phase document's acceptance for this task is four claims, and this file is
each of them once:

* a published revision reaches `applied` with **nothing hand-driven** — the
  contrast is `backend/tests/test_end_to_end_loop.py`, which drives the device
  half with `mosquitto_pub` because in E3 there was no device to drive it;
* a device that connects **after** the publish still receives it (spec 6.4's
  retained desired topic);
* an unclean death flips the Aggregator offline through the LWT (spec 9.3);
* a redelivered command runs once and a new one runs again (spec 7.4).

The platform here is the real one — migrated database, provisioned broker,
API, reconciliation worker, consumer — because every claim above is a claim
about the PLATFORM's reaction, and a stub would only prove the harness agrees
with itself. That is also why the suite imports platform internals while the
harness modules import nothing but the wire contract: `test_harness_boundaries`
enforces exactly that split.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.auth.passwords import hash_password
from app.contracts.mqtt import Command, command_topic, encode
from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.controlplane.consumer import ReportedConsumer
from app.controlplane.revision_state import RevisionState
from app.controlplane.runner import ReconciliationWorker
from app.db import create_session_factory
from app.devbroker import device_username, load_manifest
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AggregatorStatus,
    ConfigRevision,
    Deployment,
    DeploymentService,
    RoleAssignment,
    User,
)
from app.models import DeviceEvent as DeviceEventRow
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.settings import Settings
from conftest import (
    BACKEND,
    ephemeral_broker,
    ephemeral_postgres,
    free_port,
    make_kek,
)
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from device import BrokerLogin, MockAggregator

#: The seeded demo hierarchy (app.seed) is the fleet under test here; SIM.4 is
#: where a harness provisions its own over the REST API.
DEP = "redwood-coast"
AGG = "demo-agg-rc-01"
OWNER = "sim-owner@example.com"
PASSWORD = f"sim-{uuid.uuid4().hex}"

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --- a whole platform -------------------------------------------------------


@dataclass(frozen=True)
class Platform:
    """Everything a device needs to be talked to, and everything a test needs
    to check what the platform made of it."""

    app: Any
    broker: Any
    factory: Any
    store: SecretStore
    manifest: dict[str, Any]


def _provision(url: str, kek: str, out_dir: Path) -> None:
    """Mint TLS material, broker accounts, ACLs and the `deployment_service`
    row by running the platform's own generator — the same command the README
    gives an operator. The harness never writes broker credentials itself."""
    result = subprocess.run(
        [sys.executable, "-m", "app.devbroker", "--out", str(out_dir)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url, "EOE_KEK": kek, "EOE_SESSION_SECRET": "sim1"},
        timeout=180,
    )
    assert result.returncode == 0, f"devbroker failed: {result.stdout}\n{result.stderr}"


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    """A migrated database, a provisioned TLS broker, and an API whose lifespan
    has actually run, so `app.state.mqtt` is the real outbound manager."""
    out = tmp_path_factory.mktemp("sim1-certs")
    port = free_port()
    with ephemeral_postgres() as url:
        kek = make_kek()
        _, factory = create_session_factory(url)
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        _provision(url, kek, out)
        with factory() as db:
            # The broker row names `mosquitto:8883` for the compose network;
            # this test reaches the same broker on a host port, and so does the
            # device — one broker, two clients, no shortcuts.
            db.execute(update(DeploymentService).values(host="localhost", port=port))
            db.commit()
        with ephemeral_broker(out, host_port=port) as broker:
            app = create_app(
                Settings(
                    database_url=url,
                    # The SAME kek the broker secrets were wrapped with: a fresh
                    # one reads as "every broker row is unreadable", which the
                    # manager survives by design and which would make every
                    # test below quietly prove nothing.
                    kek=kek,
                    session_secret="sim1-session-secret",
                    cors_origins="",
                )
            )
            with app.state.session_factory() as db:
                user = User(email=OWNER, password_hash=hash_password(PASSWORD))
                user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
                db.add(user)
                db.commit()
            yield Platform(
                app=app,
                broker=broker,
                factory=factory,
                store=SecretStore(factory, kek),
                manifest=load_manifest(out),
            )


# --- reading the platform's mind --------------------------------------------


def operator(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def device_login(platform: Platform, aggregator_uuid: str = AGG) -> BrokerLogin:
    """The device's OWN credential, read from `accounts.json` — the one place
    dev broker passwords live. Its ACL grants seven topics and no more, so
    every test below runs against the spec 7.1 isolation as well."""
    username = device_username(aggregator_uuid)
    password = next(
        account["password"]
        for account in platform.manifest["accounts"]
        if account["username"] == username
    )
    return BrokerLogin(
        host="localhost",
        port=platform.broker.port,
        username=username,
        password=password,
        ca_cert=Path(platform.manifest["ca_cert"]),
    )


def deployment_id_of(factory, slug: str = DEP) -> uuid.UUID:
    with factory() as db:
        return db.scalars(select(Deployment.id).where(Deployment.slug == slug)).one()


def aggregator_id_of(factory, aggregator_uuid: str = AGG) -> uuid.UUID:
    with factory() as db:
        return db.scalars(
            select(Aggregator.id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()


def revision(factory, revision_id: uuid.UUID) -> tuple[str, str]:
    with factory() as db:
        row = db.execute(
            select(ConfigRevision.state, ConfigRevision.checksum).where(
                ConfigRevision.id == revision_id
            )
        ).one()
        return row.state, row.checksum


def status_row(factory, aggregator_uuid: str = AGG) -> AggregatorStatus | None:
    with factory() as db:
        return db.scalars(
            select(AggregatorStatus).where(
                AggregatorStatus.aggregator_id == aggregator_id_of(factory, aggregator_uuid)
            )
        ).first()


async def wait_for_state(factory, revision_id: uuid.UUID, wanted: RevisionState) -> None:
    for _ in range(300):
        if revision(factory, revision_id)[0] == wanted.value:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"revision stayed {revision(factory, revision_id)[0]}, wanted {wanted}")


async def wait_for_verdict(factory, *, online: bool, timeout: float = 30.0) -> AggregatorStatus:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = status_row(factory)
        if row is not None and row.online is online:
            return row
        await asyncio.sleep(0.2)
    raise AssertionError(f"the platform never recorded online={online}: {status_row(factory)}")


async def apply_change(client: TestClient, aggregator_id: uuid.UUID, verbosity: str) -> uuid.UUID:
    """Edit config the way the UI does, and publish. Returns the Aggregator's
    own revision: an aggregator-level change moves its Listeners' effective
    config too, so E2 cuts one revision per affected device and all go out."""
    body = {
        "selection": {"entity_type": "aggregator", "where": {"ids": [str(aggregator_id)]}},
        "changes": {"logging.verbosity": verbosity},
        "level": "target",
    }
    response = await asyncio.to_thread(
        client.post,
        f"{API_PREFIX}/config/apply",
        json=body,
        headers={"X-CSRF-Token": client.cookies["eoe_csrf"]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["published"] == len(result["revisions"]), "apply did not publish"
    aggregator_revision = next(r for r in result["revisions"] if r["target_type"] == "aggregator")
    return uuid.UUID(aggregator_revision["revision_id"])


def worker_for(platform: Platform) -> ReconciliationWorker:
    """The real spec 6.4 loop. The sweep intervals are pushed past the life of
    a test on purpose: a pending revision timing out mid-test would be a
    failure about patience rather than about the device."""
    return ReconciliationWorker(
        platform.factory, platform.store, timeout_interval=3600, drift_interval=3600
    )


# --- ACCEPTANCE -------------------------------------------------------------


async def test_a_published_revision_reaches_applied_with_nothing_hand_driven(platform):
    """ACCEPTANCE (phase doc SIM.1), the first claim.

    Nothing in this test touches the wire. The operator edits config, the
    platform publishes because apply asked it to, `MockAggregator` receives the
    retained desired message on its own credential, applies it and reports back
    with a checksum IT computed, and the worker's consumer moves the revision
    because that checksum matched. The device is a device, not a puppet.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        aggregator_id = aggregator_id_of(platform.factory)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            async with MockAggregator(
                deployment_slug=DEP, aggregator_uuid=AGG, login=device_login(platform)
            ) as device:
                # Spec 7.2: the device announces itself, retained.
                await wait_for_verdict(platform.factory, online=True)

                revision_id = await apply_change(client, aggregator_id, "debug")
                await device.wait_for_apply(revision_id)

                assert device.config["logging.verbosity"] == "debug"
                assert device.published_reports >= 1
                state, checksum = revision(platform.factory, revision_id)
                assert device.checksum == checksum, (
                    "the device's own checksum over what it applied did not match the "
                    "revision's — the D52 recipe and the verbatim copy are what make "
                    "these two equal by construction"
                )

                await wait_for_state(platform.factory, revision_id, RevisionState.APPLIED)

            # Leaving the context is a POLITE shutdown, and a polite shutdown
            # says so: the will is discarded on a clean DISCONNECT, so without
            # the explicit `offline` this row would read online forever.
            offline = await wait_for_verdict(platform.factory, online=False)
            assert offline.online is False


async def test_a_device_that_connects_after_the_publish_still_gets_it(platform):
    """ACCEPTANCE (phase doc SIM.1), the second claim — spec 6.4's retained
    desired topic.

    The revision is published while the fleet is dark. A device that only
    exists afterwards still converges, with no polling and nobody replaying
    anything for it, because the broker held the retained message. This is the
    property that makes an Aggregator that was in a tunnel for a week correct
    rather than lucky.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        aggregator_id = aggregator_id_of(platform.factory)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))

            revision_id = await apply_change(client, aggregator_id, "warn")

            # Only now does the device exist at all.
            async with MockAggregator(
                deployment_slug=DEP, aggregator_uuid=AGG, login=device_login(platform)
            ) as device:
                await device.wait_for_apply(revision_id)
                assert device.config["logging.verbosity"] == "warn"
                await wait_for_state(platform.factory, revision_id, RevisionState.APPLIED)


async def test_a_redelivered_command_runs_once_and_a_new_one_runs_again(platform):
    """Spec 7.4's `command_id`, from the device's side.

    Both halves of it, because each guards the other's failure: dedup by id
    means a QoS 1 redelivery restarts the device once, and dedup by id rather
    than by NAME means an operator's deliberate second restart is not silently
    swallowed. The two commands are published on the platform's own connection,
    so the ordering guarantee that makes this deterministic is MQTT's.
    """
    deployment_id = deployment_id_of(platform.factory)
    async with MockAggregator(
        deployment_slug=DEP, aggregator_uuid=AGG, login=device_login(platform)
    ) as device:
        manager = MqttClientManager(
            lambda: load_broker_coordinates(platform.factory, platform.store)
        )
        async with manager:
            await manager.wait_connected(deployment_id)
            topic = command_topic(DEP, AGG)

            first = Command(at=datetime.now(UTC), command="restart")
            await manager.publish(deployment_id, topic, encode(first), retain=False)
            await device.wait_for_command(first.command_id)

            # The same bytes again: at-least-once delivery, in practice.
            await manager.publish(deployment_id, topic, encode(first), retain=False)

            second = Command(at=datetime.now(UTC), command="restart")
            assert second.command_id != first.command_id
            await manager.publish(deployment_id, topic, encode(second), retain=False)
            await device.wait_for_command(second.command_id)

    assert device.commands_executed == ["restart", "restart"], (
        "either a redelivery ran twice, or an operator's second deliberate "
        "restart was deduplicated away"
    )


async def test_a_device_event_reaches_the_platform(platform):
    """The event topic (spec 7.3), unretained, from the device's own credential.

    E3.11 renders these on the timeline; what matters here is that the harness
    can produce one at all, because every SIM.3 scenario ends in an event the
    platform is supposed to notice.
    """
    code = f"sim_selftest_{uuid.uuid4().hex[:8]}"
    async with worker_for(platform) as worker:
        await worker.manager.wait_connected(deployment_id_of(platform.factory))
        async with MockAggregator(
            deployment_slug=DEP, aggregator_uuid=AGG, login=device_login(platform)
        ) as device:
            await device.publish_event(code, level="warn", detail="SIM.1 self-test")

            for _ in range(150):
                with platform.factory() as db:
                    row = db.scalars(
                        select(DeviceEventRow).where(DeviceEventRow.code == code)
                    ).first()
                if row is not None:
                    break
                await asyncio.sleep(0.1)
            assert row is not None, "the event never reached the platform"
            assert row.level == "warn"
            assert row.aggregator_uuid == AGG


# --- ACCEPTANCE: a real unclean death ---------------------------------------

#: A whole mock Aggregator in another process, so it can be SIGKILLed. Nothing
#: here simulates the will: the device registers a real one with a real
#: Mosquitto, and the BROKER composes and publishes the `offline` message when
#: the process disappears. That is the only way to prove the platform is
#: reading the signal spec 9.3 makes authoritative rather than one a test
#: handed it.
DETACHED_AGGREGATOR = """
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["SIM_DIR"])

from device import BrokerLogin, MockAggregator


async def main() -> None:
    login = BrokerLogin(
        host=os.environ["SIM_HOST"],
        port=int(os.environ["SIM_PORT"]),
        username=os.environ["SIM_USERNAME"],
        password=os.environ["SIM_PASSWORD"],
        ca_cert=Path(os.environ["SIM_CA_CERT"]),
    )
    async with MockAggregator(
        deployment_slug=os.environ["SIM_DEPLOYMENT"],
        aggregator_uuid=os.environ["SIM_AGGREGATOR"],
        login=login,
    ):
        print("connected", flush=True)
        await asyncio.Event().wait()


asyncio.run(main())
"""


def start_detached_aggregator(platform: Platform) -> subprocess.Popen[str]:
    login = device_login(platform)
    assert login.ca_cert is not None
    environment = {
        **os.environ,
        "SIM_DIR": str(Path(__file__).resolve().parents[1]),
        "SIM_HOST": login.host,
        "SIM_PORT": str(login.port),
        "SIM_USERNAME": login.username,
        "SIM_PASSWORD": login.password,
        "SIM_CA_CERT": str(login.ca_cert),
        "SIM_DEPLOYMENT": DEP,
        "SIM_AGGREGATOR": AGG,
    }
    return subprocess.Popen(
        [sys.executable, "-c", DETACHED_AGGREGATOR],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def test_an_unclean_death_flips_the_aggregator_offline_through_the_lwt(platform):
    """ACCEPTANCE (phase doc SIM.1), the third claim — spec 9.3 via spec 7.2.

    The device is SIGKILLed, so no DISCONNECT packet ever reaches the broker;
    that is the entire difference between this and `disconnect()`. MQTT
    publishes the will on any close that was not a DISCONNECT, and the
    platform's own consumer picks it up off the subscription it already had.
    """
    consumer = ReportedConsumer(platform.factory)
    manager = MqttClientManager(lambda: load_broker_coordinates(platform.factory, platform.store))
    manager.subscribe(consumer.filters, consumer.handle)

    async with manager:
        await manager.wait_connected(deployment_id_of(platform.factory))
        before = status_row(platform.factory)
        assert before is None or before.online is False, "a previous test left this device online"

        process = start_detached_aggregator(platform)
        try:
            online_at = (await wait_for_verdict(platform.factory, online=True)).declared_at
        finally:
            # SIGKILL. This is the test's action and its cleanup at once: no
            # DISCONNECT packet reaches the broker either way, and a device
            # left running would outlive the fixture that provisioned it.
            process.kill()
            stdout, stderr = process.communicate(timeout=30)
        assert "connected" in stdout, f"the detached device never came up:\n{stdout}\n{stderr}"

        dead = await wait_for_verdict(platform.factory, online=False)

    assert dead.declared_at < online_at, (
        "a will is composed at CONNECT time, so it is older than every `online` "
        "that followed it — which is exactly why the platform must not order "
        "the status topic by the payload clock (E3.8)"
    )
    assert dead.declared_at < dead.changed_at
