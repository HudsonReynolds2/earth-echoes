"""SIM.3 acceptance: every shipped scenario, asserted on the PLATFORM's reaction.

One test per scenario file, and each asserts what the platform ended up
believing — not what the harness did. That distinction is the task: a scenario
that ran without producing `failed`, `drifted`, an LWT offline, a Listener
offline, a `duplicate_identity` quarantine or a `provisioning_required` alert
has proved nothing, however faithfully it misbehaved.

The scenarios are loaded from the shipped TOML files rather than constructed
here. What is exercised is therefore the whole path an operator gets: the file,
the registry, the parameters as written, and the behaviour they name.

Nothing in this file writes to the database to set up a failure. The identity
cases go through the E1.5 services the way E3.5 wires them — by publishing what
a confused device would publish — and the assertions read the rows those
services produced.
"""

import uuid

import pytest
from app.controlplane.revision_state import RevisionState
from app.inventory.identity import ALERT_DUPLICATE_IDENTITY, ALERT_PROVISIONING_REQUIRED
from app.main import API_PREFIX
from app.models import DeviceState, InventoryAlert, Listener, QuarantinedReport
from conftest import (
    AGG,
    DEP,
    GHOST_AGG,
    MAC,
    OTHER_AGG,
    aggregator_id_of,
    apply_config,
    csrf,
    deployment_id_of,
    device_login,
    eventually,
    listener_state,
    operator,
    wait_for_state,
    wait_for_verdict,
    worker_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import MockAggregator
from scenarios import ScenarioContext, load_scenarios

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

#: Loaded once, from the shipped files. A load failure here fails every test in
#: the module at collection, which is the right blast radius: an unloadable
#: catalogue is not a scenario problem, it is a repository problem.
SCENARIOS = load_scenarios()

#: The demo hierarchy's smallest Pod (three Listeners), used wherever a test
#: needs an AGGREGATOR-level publish. An aggregator-level change moves every
#: Listener's effective config too, so E2 cuts a revision per device and each
#: goes out in turn — a pre-existing platform cost (see the SIM ledger), and
#: the cheapest honest way to pay it is to point at the smallest Pod.
SMALL_AGG = "demo-agg-rc-03"
SMALL_MAC = "02:EE:0E:01:03:01"


async def inject(name: str, device: MockAggregator) -> None:
    """Run one shipped scenario against one device."""
    await SCENARIOS[name].run(ScenarioContext(aggregator=device))


async def connected(platform, aggregator_uuid: str, macs: tuple[str, ...] = ()) -> MockAggregator:
    device = MockAggregator(
        deployment_slug=DEP,
        aggregator_uuid=aggregator_uuid,
        login=device_login(platform, aggregator_uuid),
    )
    for mac in macs:
        await device.add_listener(mac)
    await device.connect()
    return device


def revision_id_of(revisions, target_type: str = "aggregator") -> uuid.UUID:
    return uuid.UUID(next(r for r in revisions if r["target_type"] == target_type)["revision_id"])


async def publish_verbosity(client, aggregator_id: uuid.UUID, value: str) -> uuid.UUID:
    revisions = await apply_config(
        client,
        entity_type="aggregator",
        ids=[str(aggregator_id)],
        changes={"logging.verbosity": value},
    )
    return revision_id_of(revisions)


# --- apply errors -----------------------------------------------------------


async def test_the_apply_error_scenario_fails_the_revision(platform):
    """ACCEPTANCE (phase doc SIM.3): apply errors reach `failed`.

    The device is told to hold something else and does, reporting that config
    with its own checksum over it. The platform reads a coherent report of the
    wrong config against a `pending` revision and fails it immediately (D70) —
    rather than waiting out the pending window and recording a timeout, which
    would blame the network for a device that answered in two seconds.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        aggregator_id = aggregator_id_of(platform.factory, SMALL_AGG)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, SMALL_AGG)
            try:
                await inject("apply_error", device)

                revision_id = await publish_verbosity(client, aggregator_id, "trace")
                await device.wait_for_apply(revision_id)

                assert device.config["logging.verbosity"] == "sim-could-not-apply"
                await wait_for_state(
                    platform.factory, revision_id, RevisionState.FAILED, timeout=60.0
                )
            finally:
                await device.disconnect()


# --- drift ------------------------------------------------------------------


async def test_the_drift_scenario_moves_an_applied_revision_to_drifted(platform):
    """ACCEPTANCE (phase doc SIM.3): drift reaches `drifted`.

    Produced the way a real device produces it — the device converges first,
    honestly, and only then holds something else and says so. No row is
    touched to make this happen: if drift could not be produced from the
    device's side, drift detection would not work in the field either.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        aggregator_id = aggregator_id_of(platform.factory, SMALL_AGG)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, SMALL_AGG)
            try:
                revision_id = await publish_verbosity(client, aggregator_id, "debug")
                await device.wait_for_apply(revision_id)
                await wait_for_state(
                    platform.factory, revision_id, RevisionState.APPLIED, timeout=60.0
                )

                await inject("drift", device)

                assert device.config["logging.verbosity"] == "trace"
                await wait_for_state(
                    platform.factory, revision_id, RevisionState.DRIFTED, timeout=60.0
                )
            finally:
                await device.disconnect()


# --- disconnects ------------------------------------------------------------


async def test_the_disconnect_scenario_lands_as_an_lwt_offline(platform):
    """ACCEPTANCE (phase doc SIM.3): a disconnect reaches the platform as
    offline, through the will.

    The device says nothing on its way out — the socket simply goes — so the
    `offline` the platform records was composed by the broker from the will
    registered at CONNECT. That is the signal spec 9.3 makes authoritative,
    and a scenario that published `offline` itself would be testing the
    device's manners rather than the platform's outage detection.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, AGG)
            online = await wait_for_verdict(platform.factory, online=True, aggregator_uuid=AGG)

            await inject("disconnect", device)

            dead = await wait_for_verdict(platform.factory, online=False, aggregator_uuid=AGG)
            assert dead.declared_at < online.declared_at, (
                "a will is composed at CONNECT time, so it is older than every `online` "
                "that followed it — an `offline` newer than that would mean the device "
                "published it politely and this scenario proved nothing"
            )


# --- missed wake windows ----------------------------------------------------


async def test_the_missed_wake_window_scenario_takes_the_listener_offline(platform):
    """ACCEPTANCE (phase doc SIM.3): a missed wake window reaches the platform
    as an offline Listener AND as an event on the timeline.

    The scenario file makes the promise and stops. The grace period is the
    device's, from its own config, and the wait below is therefore genuinely
    the spec 5.3 default — the scenario cannot shorten it, which is the point:
    if it could, it would be the harness deciding a wake window was missed.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, SMALL_AGG, macs=(SMALL_MAC,))
            listener = device.listener(SMALL_MAC)
            try:
                await inject("missed_wake_window", device)
                assert listener.state == "sleeping"

                await listener.wait_for_liveness("offline", timeout=90.0)

                offline = await eventually(
                    lambda: is_offline(platform, SMALL_MAC),
                    what=f"recorded {SMALL_MAC} offline",
                    timeout=60.0,
                )
                assert offline.expected_wake_at is None
                assert timeline_events(platform, SMALL_MAC)(), (
                    "the Listener went offline with nothing on the timeline to explain it"
                )
            finally:
                await device.disconnect()


def is_offline(platform, mac: str):
    row = listener_state(platform.factory, mac)
    return row if row is not None and row.liveness_state == "offline" else None


def timeline_events(platform, mac: str):
    from app.models import DeviceEvent as DeviceEventRow

    def probe():
        with platform.factory() as db:
            return db.scalars(
                select(DeviceEventRow).where(DeviceEventRow.listener_mac == mac)
            ).all()

    return probe


# --- duplicate identity -----------------------------------------------------


def listener_row(platform, mac: str = MAC) -> tuple:
    """One Listener's inventory row, as a comparable tuple. Every column the
    identity path could conceivably rewrite, so "unchanged" is provable rather
    than asserted about the one field a test happened to look at."""
    with platform.factory() as db:
        row = db.get(Listener, mac)
        assert row is not None
        return (
            row.mac,
            row.name,
            row.aggregator_id,
            row.deployment_id,
            row.gps_lat,
            row.gps_lon,
            tuple(row.tags),
        )


async def test_the_duplicate_mac_scenario_quarantines_and_leaves_inventory_alone(platform):
    """ACCEPTANCE (phase doc SIM.3): `duplicate_identity` quarantine with
    inventory PROVABLY unchanged.

    Spec 4.3 item 2 is emphatic that the report is quarantined "rather than
    overwriting inventory", and this is the test that would notice the day
    that stopped being true: the Listener's whole row is captured before and
    compared after. A platform that let the loudest device rewrite inventory
    would make a mis-flashed Listener silently steal another's identity, and
    the operator's record of what is where would quietly become fiction.
    """
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            before = listener_row(platform)
            device = await connected(platform, OTHER_AGG)
            try:
                await inject("duplicate_mac", device)

                quarantined = await eventually(
                    quarantine_for(platform, MAC), what=f"quarantined the report for {MAC}"
                )
                assert quarantined.reason == "mac_conflict"
                assert quarantined.aggregator_uuid == OTHER_AGG

                alert = await eventually(
                    alert_for(platform, ALERT_DUPLICATE_IDENTITY, MAC),
                    what="opened a duplicate_identity alert",
                )
                assert alert.resolved_at is None

                assert listener_row(platform) == before, (
                    "the report rewrote inventory; spec 4.3 item 2 says the conflicting "
                    "REPORT is quarantined and the row is left exactly as it was"
                )
                assert listener_state(platform.factory, MAC) is None, (
                    "a report the platform does not believe became the Listener's reported state"
                )
            finally:
                await device.disconnect()


def quarantine_for(platform, mac: str):
    def probe():
        with platform.factory() as db:
            return db.scalars(select(QuarantinedReport).where(QuarantinedReport.mac == mac)).first()

    return probe


def alert_for(platform, alert_type: str, entity_key: str):
    def probe():
        with platform.factory() as db:
            return db.scalars(
                select(InventoryAlert).where(
                    InventoryAlert.alert_type == alert_type,
                    InventoryAlert.entity_key == entity_key,
                    InventoryAlert.resolved_at.is_(None),
                )
            ).first()

    return probe


# --- unprovisioned reporters ------------------------------------------------


async def test_the_unprovisioned_scenario_opens_a_provisioning_required_alert(platform):
    """ACCEPTANCE (phase doc SIM.3): an unprovisioned `aggregator_uuid` reaches
    `provisioning_required`.

    The device is real: it holds a broker credential minted for it, because a
    device with no credential could not connect at all and would prove nothing
    about the platform. What it no longer holds is an inventory row — an
    operator deleted it here, through the API, exactly as a decommissioning
    would. Spec 4.3 item 3's check is MEMBERSHIP against inventory, so the
    hardware in the field is now unknown, and everything it says is answered
    with an alert instead of a state row.
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        ghost_id = aggregator_id_of(platform.factory, GHOST_AGG)
        deleted = client.delete(f"{API_PREFIX}/aggregators/{ghost_id}", headers=csrf(client))
        assert deleted.status_code == 204, deleted.text

        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            device = await connected(platform, GHOST_AGG)
            try:
                await inject("unprovisioned_aggregator", device)

                alert = await eventually(
                    alert_for(platform, ALERT_PROVISIONING_REQUIRED, GHOST_AGG),
                    what=f"opened a provisioning_required alert for {GHOST_AGG}",
                )
                assert alert.entity_type == "aggregator"

                with platform.factory() as db:
                    stored = db.scalars(
                        select(DeviceState).where(DeviceState.entity_id == str(ghost_id))
                    ).first()
                assert stored is None, (
                    "the platform stored reported state for a device no inventory row "
                    "vouches for; the membership check is what stops a stranger's "
                    "config from becoming a record"
                )
            finally:
                await device.disconnect()
