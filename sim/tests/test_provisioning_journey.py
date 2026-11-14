"""E4.9 acceptance, end to end against a live mock Aggregator (phase-4 E4.9;
spec 8.5; D179).

The journey: an operator generates a bundle, marks the card flashed, the device
comes up on its own credential, takes its retained desired config from the
broker, applies it, and reports. The record moves because the PLATFORM saw it,
not because a test said so. Nothing here writes E3's evidence tables: the
device's real report does.

1. **The card's revision is still current at first boot → `confirmed`**, and
   the confirmation latches through a later legitimate config change.
2. **A card generated before a config change → `registered`**, not
   `mismatch`: a later revision is not a disagreement (spec 8.5's false-alarm
   rule).
3. **A device reporting a conflicting MAC → `mismatch`**, with inventory
   provably unchanged.

1 and 2 share one connected session to spend two config applies rather than
four (each apply is E3's full round trip; see the first test's docstring).

On "registered then confirmed": the mock reports only after applying, so when
the embedded revision is still current its first report already carries the
embedded checksum, and the record goes `flashed → confirmed` in one step.
`registered` is the state for a device on a different-but-known revision,
which test 2 produces for real. D179 records this reading of the phase
document's wording.
"""

import pytest
from app.config.overrides import put_overrides
from app.models import Aggregator, Deployment, DeviceProvisioningRecord, Listener, Pod
from app.provisioning.envelope import FirmwareKey
from app.provisioning.records import mark_flashed, reconcile_records
from app.provisioning.service import PodChoice, create_bundle
from app.services.credentials import DeviceCredential
from conftest import (
    AGG,
    DEP,
    MAC,
    OTHER_AGG,
    aggregator_id_of,
    apply_change,
    deployment_id_of,
    device_login,
    eventually,
    operator,
    worker_for,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from device import MockAggregator
from scenarios import ScenarioContext, load_scenarios

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SMALL_AGG = "demo-agg-rc-03"
SCENARIOS = load_scenarios()

NETWORK = {
    "network.wifi_ssid": "sim-halow",
    "network.wifi_password": "sim-journey-psk-31c0",
    "network.stream_key": "sim-journey-stream-77ab",
    "network.aggregator_ip": "10.88.0.1",
    "network.stream_endpoint": "10.88.0.1:8765/stream",
}


class NoBroker:
    """Bundles here are generated with credentials off: the mock dials with
    the login `app.devbroker` minted for it, which is what a real Pi's
    settings.yaml would carry."""

    async def mint(self, coordinates, aggregator_uuid):  # pragma: no cover
        return DeviceCredential(username="unused", password="unused-in-the-journey")

    async def revoke(self, coordinates, aggregator_uuid):  # pragma: no cover
        raise AssertionError


async def _bundle_for(platform, aggregator_uuid: str) -> list[DeviceProvisioningRecord]:
    """What the operator does: configure the Pod's network, have a verified
    broker, generate a pod bundle."""
    with platform.factory() as db:
        pod_id = db.scalars(
            select(Aggregator.pod_id).where(Aggregator.aggregator_uuid == aggregator_uuid)
        ).one()
        pod = db.get(Pod, pod_id)
        put_overrides(db, platform.store, "pod", str(pod.id), NETWORK)
        db.get(Deployment, pod.deployment_id).services_status = "verified"
        db.commit()
        _, records = await create_bundle(
            db,
            platform.factory,
            platform.store,
            NoBroker(),
            FirmwareKey.from_base64(platform.kek, 1),
            deployment_id=pod.deployment_id,
            scope="pod",
            choices=[PodChoice(pod_id=pod.id)],
            include_credentials=False,
            actor_user_id=None,
        )
        return records


def _record(platform, target_type: str, target_id: str, records) -> DeviceProvisioningRecord:
    wanted = next(r for r in records if r.target_type == target_type and r.target_id == target_id)
    with platform.factory() as db:
        return db.get(DeviceProvisioningRecord, wanted.id)


def _settles_at(platform, record_id, status: str):
    """A probe that runs the reconciler (what the worker's sweep does) and
    reports whether the record has reached `status`."""

    def probe():
        reconcile_records(platform.factory)
        with platform.factory() as db:
            record = db.get(DeviceProvisioningRecord, record_id)
            return record if record.status == status else None

    return probe


async def test_first_boot_confirms_a_later_change_registers_and_confirmed_latches(
    platform,
):
    """Three of the phase document's claims in one connected session, because
    each config apply costs E3's full publish-and-ack round trip (about 40s on
    the smallest Pod, the SIM ledger's known cost). Two applies cover all three
    claims:

    1. apply X, generate bundle 1 (embeds X), flash it, connect: the device
       applies X, so bundle 1 is **confirmed**;
    2. still connected, generate bundle 2 (also embeds X), apply Y: the device
       now reports Y, a LATER known revision, so bundle 2 is **registered and
       never mismatch**, and bundle 1 **stays confirmed** (the latch).
    """
    with TestClient(platform.app):
        client = operator(platform.app)
        aggregator_id = aggregator_id_of(platform.factory, SMALL_AGG)
        target = str(aggregator_id)
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            # The config the first card embeds, retained at the broker before
            # the device has ever connected.
            current = await apply_change(client, aggregator_id, "debug")
            first = _record(platform, "aggregator", target, await _bundle_for(platform, SMALL_AGG))
            assert first.config_revision_id == current

            with platform.factory() as db:
                mark_flashed(db.get(DeviceProvisioningRecord, first.id), platform_actor(db))
                db.commit()
            reconcile_records(platform.factory)
            assert _status(platform, first.id) == "flashed", "no device yet: nothing to observe"

            async with MockAggregator(
                deployment_slug=DEP,
                aggregator_uuid=SMALL_AGG,
                login=device_login(platform, SMALL_AGG),
            ) as device:
                await device.wait_for_apply(current)
                confirmed = await eventually(
                    _settles_at(platform, first.id, "confirmed"), what="confirmed the first card"
                )
                assert [e["status"] for e in confirmed.status_history] == [
                    "generated",
                    "flashed",
                    "confirmed",
                ]

                second = _record(
                    platform, "aggregator", target, await _bundle_for(platform, SMALL_AGG)
                )
                assert second.config_revision_id == current
                later = await apply_change(client, aggregator_id, "warn")
                await device.wait_for_apply(later)

                registered = await eventually(
                    _settles_at(platform, second.id, "registered"),
                    what="registered the second card",
                )
                assert "mismatch" not in [e["status"] for e in registered.status_history]
                assert _status(platform, first.id) == "confirmed", "confirmation latches"


async def test_a_device_reporting_a_conflicting_mac_lands_in_mismatch_and_inventory_is_unchanged(
    platform,
):
    with TestClient(platform.app):
        async with worker_for(platform) as worker:
            await worker.manager.wait_connected(deployment_id_of(platform.factory))
            records = await _bundle_for(platform, AGG)
            card = _record(platform, "listener", MAC, records)
            with platform.factory() as db:
                listener = db.get(Listener, MAC)
                before = (listener.name, listener.aggregator_id, listener.gps_lat, listener.gps_lon)

            # A different Aggregator claims this Listener's MAC (the shipped
            # `duplicate_mac` scenario), which E3 quarantines as mac_conflict.
            device = MockAggregator(
                deployment_slug=DEP,
                aggregator_uuid=OTHER_AGG,
                login=device_login(platform, OTHER_AGG),
            )
            await device.connect()
            try:
                await SCENARIOS["duplicate_mac"].run(ScenarioContext(aggregator=device))
                await eventually(
                    _settles_at(platform, card.id, "mismatch"), what="flagged the card mismatch"
                )
            finally:
                await device.disconnect()

            with platform.factory() as db:
                listener = db.get(Listener, MAC)
                after = (listener.name, listener.aggregator_id, listener.gps_lat, listener.gps_lon)
            assert after == before, "a conflicting report must never rewrite inventory"


def _status(platform, record_id) -> str:
    with platform.factory() as db:
        return db.get(DeviceProvisioningRecord, record_id).status


def platform_actor(db):
    """The seeded owner, standing in for the operator who marks the card."""
    from app.models import User
    from conftest import OWNER

    return db.scalars(select(User.id).where(User.email == OWNER)).one()
