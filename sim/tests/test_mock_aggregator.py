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
enforces exactly that split. The fixture that assembles all of it, and the
helpers that read its mind, moved to `conftest.py` when SIM.2 became the second
module to need them.
"""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.contracts.mqtt import Command, command_topic, encode
from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.controlplane.consumer import ReportedConsumer
from app.controlplane.revision_state import RevisionState
from app.models import DeviceEvent as DeviceEventRow
from conftest import (
    AGG,
    DEP,
    Platform,
    aggregator_id_of,
    apply_change,
    deployment_id_of,
    device_login,
    eventually,
    operator,
    revision,
    status_row,
    wait_for_state,
    wait_for_verdict,
    worker_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import MockAggregator

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


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

            def stored():
                with platform.factory() as db:
                    return db.scalars(
                        select(DeviceEventRow).where(DeviceEventRow.code == code)
                    ).first()

            row = await eventually(stored, what=f"stored the {code} event")
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
