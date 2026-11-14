"""E4.9: the record reconciler's rules (spec 8.5; phase-4 E4.9; D168, D179).

Each rule from the phase document gets a test, against real bundles built by
`create_bundle` and E3-shaped evidence rows:

- no evidence leaves `generated`, and the operator's `flashed` survives a sweep;
- a `device_state` row whose checksum is the embedded revision's → `confirmed`,
  **and it latches**: a later legitimate config change does not undo it;
- a checksum belonging to a LATER revision → `registered`, never `mismatch`;
- a checksum belonging to NO revision for the device → `mismatch`, which
  re-derives away when the evidence clears;
- an identity quarantine for the MAC created after the record → `mismatch`,
  inventory untouched, while one from BEFORE the card existed is history;
- the Aggregator's record is keyed by its row id (D75) and confirms the same
  way;
- **idempotent and restart-safe**: a second pass changes nothing, rows diffed;
- **the worker's registered sweep** does this on its own (D168).

The end-to-end journey with a live mock Aggregator is
`sim/tests/test_provisioning_journey.py`.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select, update
from test_reconciliation_worker import StubManager

from app.config.overrides import put_overrides
from app.controlplane.runner import ReconciliationWorker
from app.db import create_session_factory
from app.devbroker import plan_accounts, register_services
from app.models import (
    Aggregator,
    ConfigRevision,
    Deployment,
    DeviceProvisioningRecord,
    DeviceState,
    Listener,
    Pod,
    QuarantinedReport,
)
from app.provisioning.envelope import FirmwareKey
from app.provisioning.records import (
    RecordTransitionError,
    derive_status,
    mark_flashed,
    reconcile_records,
    unmark_flashed,
)
from app.provisioning.service import create_bundle
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.services.credentials import DeviceCredential

pytestmark = pytest.mark.integration

CA = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
NETWORK = {
    "network.wifi_ssid": "e49-halow",
    "network.wifi_password": "e49-planted-psk-0b3f1d",
    "network.stream_key": "e49-planted-stream-7c2a90",
    "network.aggregator_ip": "10.49.0.1",
    "network.stream_endpoint": "10.49.0.1:8765/stream",
}
ACTOR = uuid.uuid4()


class FakeProvider:
    async def mint(self, coordinates, aggregator_uuid):  # pragma: no cover
        return DeviceCredential(username="u", password="never-used-here")

    async def revoke(self, coordinates, aggregator_uuid):  # pragma: no cover
        raise AssertionError


def _checksum(digit: str) -> str:
    return "sha256:" + digit * 64


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def env():
    """A bundle per demo Pod in the first deployment, each Listener and
    Aggregator given an embedded revision ('1…') before generation."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            for pod in db.scalars(select(Pod)):
                put_overrides(db, store, "pod", str(pod.id), NETWORK)
            db.commit()
            register_services(
                db, store, plan_accounts(db), host="broker.example", port=8883, ca_cert_pem=CA
            )
            db.execute(update(Deployment).values(services_status="verified"))
            deployment = db.scalars(select(Deployment).order_by(Deployment.slug)).first()
            pods = list(
                db.scalars(
                    select(Pod)
                    .join(Aggregator, Aggregator.pod_id == Pod.id)
                    .where(Pod.deployment_id == deployment.id)
                    .order_by(Pod.name)
                )
            )
            earlier = datetime.now(UTC) - timedelta(days=1)
            for pod in pods:
                aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
                targets = [("aggregator", str(aggregator.id))] + [
                    ("listener", mac)
                    for mac in db.scalars(
                        select(Listener.mac).where(Listener.aggregator_id == aggregator.id)
                    )
                ]
                for target_type, target_id in targets:
                    db.add(
                        ConfigRevision(
                            target_type=target_type,
                            target_id=target_id,
                            deployment_id=deployment.id,
                            snapshot={},
                            checksum=_checksum("1"),
                            created_at=earlier,
                        )
                    )
            db.commit()
            deployment_id = deployment.id
        with factory() as db:
            asyncio.run(
                create_bundle(
                    db,
                    factory,
                    store,
                    FakeProvider(),
                    FirmwareKey.from_base64(make_kek(), 1),
                    deployment_id=deployment_id,
                    scope="deployment",
                    choices=[],
                    include_credentials=False,
                    actor_user_id=None,
                )
            )
        yield factory, deployment_id


def _records(factory, target_type: str) -> list[DeviceProvisioningRecord]:
    with factory() as db:
        return list(
            db.scalars(
                select(DeviceProvisioningRecord)
                .where(DeviceProvisioningRecord.target_type == target_type)
                .order_by(DeviceProvisioningRecord.target_id)
            )
        )


def _status(factory, record_id) -> tuple[str, list[str]]:
    with factory() as db:
        record = db.get(DeviceProvisioningRecord, record_id)
        return record.status, [entry["status"] for entry in record.status_history]


def _report(factory, deployment_id, target_type, target_id, checksum) -> None:
    """What E3's consumer writes on a report: one row per device, replaced."""
    with factory() as db:
        row = db.scalar(
            select(DeviceState).where(
                DeviceState.entity_type == target_type, DeviceState.entity_id == target_id
            )
        )
        if row is None:
            row = DeviceState(
                entity_type=target_type, entity_id=target_id, deployment_id=deployment_id
            )
            db.add(row)
        row.reported_at = datetime.now(UTC)
        row.checksum = checksum
        row.config = {}
        db.commit()


def _revision(factory, deployment_id, target_type, target_id, digit) -> None:
    with factory() as db:
        db.add(
            ConfigRevision(
                target_type=target_type,
                target_id=target_id,
                deployment_id=deployment_id,
                snapshot={},
                checksum=_checksum(digit),
            )
        )
        db.commit()


# --- The rules -----------------------------------------------------------------------


def test_no_evidence_stays_generated_and_flashed_survives_a_sweep(env):
    factory, _ = env
    record = _records(factory, "listener")[0]
    assert _status(factory, record.id) == ("generated", ["generated"])

    with factory() as db:
        assert mark_flashed(db.get(DeviceProvisioningRecord, record.id), ACTOR) is True
        db.commit()
    reconcile_records(factory)
    assert _status(factory, record.id) == ("flashed", ["generated", "flashed"])


def test_the_embedded_checksum_confirms_and_the_confirmation_latches(env):
    factory, deployment_id = env
    record = _records(factory, "listener")[1]
    _report(factory, deployment_id, "listener", record.target_id, record.config_checksum)
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "confirmed"

    # The operator changes the config later and the device applies it: the
    # record must NOT fall out of confirmed (spec 8.5, first-boot statement).
    _revision(factory, deployment_id, "listener", record.target_id, "2")
    _report(factory, deployment_id, "listener", record.target_id, _checksum("2"))
    reconcile_records(factory)
    assert _status(factory, record.id) == ("confirmed", ["generated", "confirmed"])


def test_a_later_revisions_checksum_registers_and_is_not_a_mismatch(env):
    factory, deployment_id = env
    record = _records(factory, "listener")[2]
    _revision(factory, deployment_id, "listener", record.target_id, "3")
    _report(factory, deployment_id, "listener", record.target_id, _checksum("3"))
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "registered"


def test_an_unknown_checksum_is_a_mismatch_that_clears_with_the_evidence(env):
    factory, deployment_id = env
    record = _records(factory, "listener")[3]
    _report(factory, deployment_id, "listener", record.target_id, _checksum("f"))
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "mismatch"

    _report(factory, deployment_id, "listener", record.target_id, record.config_checksum)
    reconcile_records(factory)
    status, history = _status(factory, record.id)
    assert status == "confirmed"
    assert history == ["generated", "mismatch", "confirmed"]


def test_an_identity_quarantine_after_the_card_is_a_mismatch_and_inventory_is_untouched(env):
    factory, _ = env
    record = _records(factory, "listener")[4]
    with factory() as db:
        before = db.get(Listener, record.target_id)
        snapshot = (before.name, before.aggregator_id, before.gps_lat, before.gps_lon)
        db.add(
            QuarantinedReport(
                mac=record.target_id,
                reason="mac_conflict",
                aggregator_uuid="some-other-aggregator",
                report={"mac": record.target_id},
            )
        )
        db.commit()
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "mismatch"
    with factory() as db:
        after = db.get(Listener, record.target_id)
        assert (after.name, after.aggregator_id, after.gps_lat, after.gps_lon) == snapshot


def test_a_quarantine_from_before_the_card_existed_is_history(env):
    factory, _ = env
    record = _records(factory, "listener")[5]
    with factory() as db:
        db.add(
            QuarantinedReport(
                mac=record.target_id,
                reason="unknown_mac",
                report={"mac": record.target_id},
                created_at=record.created_at - timedelta(days=2),
            )
        )
        db.commit()
        assert derive_status(db, db.get(DeviceProvisioningRecord, record.id)) == "generated"


def test_the_aggregator_record_is_keyed_by_row_id_and_confirms(env):
    factory, deployment_id = env
    record = _records(factory, "aggregator")[0]
    with factory() as db:
        assert db.get(Aggregator, uuid.UUID(record.target_id)) is not None, "D75 keying"
    _report(factory, deployment_id, "aggregator", record.target_id, record.config_checksum)
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "confirmed"


# --- Idempotence, restart-safety, and the operator transition ---------------------------------


def _all_rows(factory) -> list[tuple]:
    with factory() as db:
        return [
            (r.id, r.status, tuple(e["status"] for e in r.status_history))
            for r in db.scalars(
                select(DeviceProvisioningRecord).order_by(DeviceProvisioningRecord.id)
            )
        ]


def test_the_reconciler_is_idempotent_and_restart_safe(env):
    factory, _ = env
    reconcile_records(factory)
    first = _all_rows(factory)
    second_report = reconcile_records(factory)  # a "restarted" worker's first pass
    assert second_report.changed is False
    assert _all_rows(factory) == first


def test_the_operator_may_only_flash_a_generated_card(env):
    factory, _ = env
    confirmed = next(r for r in _records(factory, "listener") if r.status == "confirmed")
    with factory() as db:
        with pytest.raises(RecordTransitionError, match="'confirmed'"):
            mark_flashed(db.get(DeviceProvisioningRecord, confirmed.id), ACTOR)
        flashed = next(r for r in _records(factory, "listener") if r.status == "flashed")
        assert mark_flashed(db.get(DeviceProvisioningRecord, flashed.id), ACTOR) is False


def test_an_unmarked_card_stays_generated_through_a_sweep(env):
    """D187: a mis-click is undone, and the sweep honours the operator's LATEST
    word. Before, it re-derived `flashed` from any mark ever made, so an
    unmarked card would have jumped back within one pass."""
    factory, _ = env
    record = next(r for r in _records(factory, "listener") if r.status == "generated")
    history = list(record.status_history)
    with factory() as db:
        assert mark_flashed(db.get(DeviceProvisioningRecord, record.id), ACTOR) is True
        db.commit()
    with factory() as db:
        assert unmark_flashed(db.get(DeviceProvisioningRecord, record.id), ACTOR) is True
        db.commit()
    assert reconcile_records(factory).changed is False
    assert _status(factory, record.id) == (
        "generated",
        [e["status"] for e in history] + ["flashed", "generated"],
    )

    with factory() as db:  # and it can be marked again, which the sweep keeps
        assert unmark_flashed(db.get(DeviceProvisioningRecord, record.id), ACTOR) is False
        assert mark_flashed(db.get(DeviceProvisioningRecord, record.id), ACTOR) is True
        db.commit()
    reconcile_records(factory)
    assert _status(factory, record.id)[0] == "flashed"


def test_the_operator_may_only_unmark_a_flashed_card(env):
    factory, _ = env
    confirmed = next(r for r in _records(factory, "listener") if r.status == "confirmed")
    with factory() as db, pytest.raises(RecordTransitionError, match="'confirmed'"):
        unmark_flashed(db.get(DeviceProvisioningRecord, confirmed.id), ACTOR)


# --- The worker runs it (D168) ------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_workers_registered_sweep_reconciles_on_its_own(env):
    factory, deployment_id = env
    record = _records(factory, "aggregator")[1]
    _report(factory, deployment_id, "aggregator", record.target_id, record.config_checksum)
    worker = ReconciliationWorker(
        factory,
        SecretStore(factory, make_kek()),
        manager=StubManager(),
        timeout_interval=3600,
        drift_interval=3600,
        service_config_interval=0.1,
    )
    async with worker:
        for _ in range(100):
            if _status(factory, record.id)[0] == "confirmed":
                break
            await asyncio.sleep(0.1)
    assert _status(factory, record.id)[0] == "confirmed"
    assert worker.counters.sweep_failures == 0
