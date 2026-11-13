"""E4.1: the provisioning bundle and record tables (spec 8.5; D33/D55; D170).

Three things are pinned here, because losing any of them silently would be
expensive:

1. **The record status vocabulary is spec 8.5's**, transcribed by hand below,
   the way `test_settings_catalog.py` pins CATALOG against spec 5.3. The CHECK
   constraint is rendered from `models.RECORD_STATUSES`, and the database is
   asserted to accept exactly those five and refuse anything else.
2. **A record outlives the device it describes.** Deleting a Listener, then
   its Aggregator and Pod, leaves the records readable with every field intact.
   A record is evidence of what was prepared for a card that may already be in
   a tree, so a cascade here would erase exactly the history spec 8.5 exists
   to keep.
3. **The bundle row is evidence too:** deleting the user who generated it
   keeps the bundle and clears `generated_by`, never the reverse.
"""

import uuid

import pytest
from conftest import ephemeral_postgres
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.db import create_session_factory
from app.models import (
    BUNDLE_MODES,
    RECORD_FILE_KINDS,
    RECORD_STATUSES,
    Aggregator,
    Deployment,
    DeviceProvisioningRecord,
    Listener,
    Pod,
    ProvisioningBundle,
    User,
)
from app.provisioning import RECORD_STATUSES as REEXPORTED_STATUSES
from app.seed import seed_demo_hierarchy

pytestmark = pytest.mark.integration

#: **Spec 8.5, transcribed by hand**: "Statuses: `generated`, `flashed`
#: (operator marked the card written), `registered` (device connected and
#: registered by MAC), `confirmed` (reported state matches the embedded
#: revision), `mismatch` (registered device reports unexpected identity or
#: config)." If a spec revision changes this list, this line changes with it
#: and `models.RECORD_STATUSES` must follow; that is the alarm.
SPEC_8_5_STATUSES = ("generated", "flashed", "registered", "confirmed", "mismatch")

#: Spec 8.2's two modes and the mix it permits ("Operators pick per Pod, and
#: may mix the two").
SPEC_8_2_MODES = ("per_pod", "per_listener", "mixed")


@pytest.fixture(scope="module")
def factory():
    with ephemeral_postgres() as url:
        _, session_factory = create_session_factory(url)
        with session_factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield session_factory


def _first_pod(db):
    """A demo Pod with its Aggregator and at least one Listener."""
    pod = db.scalars(select(Pod).order_by(Pod.name)).first()
    aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod.id))
    listeners = list(
        db.scalars(
            select(Listener).where(Listener.aggregator_id == aggregator.id).order_by(Listener.mac)
        )
    )
    assert listeners, "the demo hierarchy should give every Pod some Listeners"
    return pod, aggregator, listeners


def _bundle(db, pod, generated_by=None) -> ProvisioningBundle:
    bundle = ProvisioningBundle(
        deployment_id=pod.deployment_id,
        scope_type="pod",
        scope_id=pod.id,
        plan={str(pod.id): {"mode": "per_pod", "listener_macs": []}},
        template_version=1,
        firmware_key_version=1,
        archive_checksum="sha256:" + "0" * 64,
        generated_by=generated_by,
    )
    db.add(bundle)
    db.flush()
    return bundle


def _record(db, bundle, pod, aggregator, *, target_type, target_id, status="generated"):
    record = DeviceProvisioningRecord(
        bundle_id=bundle.id,
        deployment_id=pod.deployment_id,
        pod_id=pod.id,
        aggregator_uuid=aggregator.aggregator_uuid,
        target_type=target_type,
        target_id=target_id,
        file_kind="settings_yaml" if target_type == "aggregator" else "pod_file",
        file_path="pod/device_config.yaml",
        status=status,
    )
    db.add(record)
    db.flush()
    return record


# --- 1. The vocabulary is the spec's ------------------------------------------


def test_record_statuses_are_the_spec_8_5_list_in_order():
    assert RECORD_STATUSES == SPEC_8_5_STATUSES
    assert REEXPORTED_STATUSES is RECORD_STATUSES


def test_bundle_modes_are_spec_8_2s_and_file_kinds_cover_both_device_types():
    assert BUNDLE_MODES == SPEC_8_2_MODES
    assert set(RECORD_FILE_KINDS) == {"pod_file", "listener_file", "settings_yaml"}


def test_the_database_accepts_every_spec_status_and_refuses_a_sixth(factory):
    with factory() as db:
        pod, aggregator, listeners = _first_pod(db)
        bundle = _bundle(db, pod)
        for index, status in enumerate(SPEC_8_5_STATUSES):
            _record(
                db, bundle, pod, aggregator,
                target_type="listener", target_id=f"02:00:00:00:00:{index:02X}", status=status,
            )  # fmt: skip
        db.commit()

    with factory() as db:
        pod, aggregator, _ = _first_pod(db)
        bundle = _bundle(db, pod)
        with pytest.raises(IntegrityError, match="status_vocab"):
            _record(
                db, bundle, pod, aggregator,
                target_type="listener", target_id="02:00:00:00:00:FF", status="lost",
            )  # fmt: skip
        db.rollback()


def test_target_type_and_scope_type_are_constrained(factory):
    with factory() as db:
        pod, aggregator, _ = _first_pod(db)
        bundle = _bundle(db, pod)
        with pytest.raises(IntegrityError, match="target_type_vocab"):
            _record(db, bundle, pod, aggregator, target_type="pod", target_id=str(pod.id))
        db.rollback()

    with factory() as db:
        pod, _, _ = _first_pod(db)
        db.add(
            ProvisioningBundle(
                deployment_id=pod.deployment_id,
                scope_type="organization",
                scope_id=pod.id,
                plan={},
                template_version=1,
                firmware_key_version=1,
                archive_checksum="sha256:" + "0" * 64,
            )
        )
        with pytest.raises(IntegrityError, match="scope_type_vocab"):
            db.flush()
        db.rollback()


def test_one_record_per_device_per_bundle(factory):
    with factory() as db:
        pod, aggregator, listeners = _first_pod(db)
        bundle = _bundle(db, pod)
        _record(db, bundle, pod, aggregator, target_type="listener", target_id=listeners[0].mac)
        with pytest.raises(IntegrityError, match="uq_device_provisioning_record_bundle_id"):
            _record(db, bundle, pod, aggregator, target_type="listener", target_id=listeners[0].mac)
        db.rollback()


def test_a_new_record_starts_generated_with_an_empty_history(factory):
    """The server defaults, asserted through raw SQL so the ORM's Python-side
    defaults cannot stand in for them."""
    with factory() as db:
        pod, aggregator, _ = _first_pod(db)
        bundle = _bundle(db, pod)
        record_id = uuid.uuid4()
        db.execute(
            text(
                "INSERT INTO device_provisioning_record (id, bundle_id, deployment_id, pod_id, "
                "aggregator_uuid, target_type, target_id, file_kind, file_path) VALUES "
                "(:id, :bundle, :dep, :pod, :agg, 'listener', '02:00:00:00:01:01', 'pod_file', "
                "'pod/device_config.yaml')"
            ),
            {
                "id": record_id,
                "bundle": bundle.id,
                "dep": pod.deployment_id,
                "pod": pod.id,
                "agg": aggregator.aggregator_uuid,
            },
        )
        status, history = db.execute(
            text("SELECT status, status_history FROM device_provisioning_record WHERE id = :id"),
            {"id": record_id},
        ).one()
        assert (status, history) == ("generated", [])
        db.rollback()


def test_status_history_round_trips(factory):
    entries = [
        {"status": "generated", "at": "2026-11-13T12:00:00+00:00", "actor": None},
        {"status": "flashed", "at": "2026-11-13T12:05:00+00:00", "actor": str(uuid.uuid4())},
    ]
    with factory() as db:
        pod, aggregator, listeners = _first_pod(db)
        bundle = _bundle(db, pod)
        record = _record(
            db, bundle, pod, aggregator, target_type="listener", target_id=listeners[-1].mac
        )
        record.status = "flashed"
        record.status_history = entries
        db.commit()
        record_id = record.id

    with factory() as db:
        stored = db.get(DeviceProvisioningRecord, record_id)
        assert stored.status == "flashed"
        assert stored.status_history == entries


# --- 2. Records outlive their devices -----------------------------------------


def test_deleting_the_devices_leaves_their_records_intact_and_readable(factory):
    """Delete a Listener, then the rest of its Pod's inventory. The records for
    both device types survive with every field as written."""
    with factory() as db:
        # A Pod of its own, so deleting it disturbs no other test.
        deployment = db.scalars(select(Deployment).order_by(Deployment.slug)).first()
        pod = Pod(deployment_id=deployment.id, name="E4.1 doomed pod")
        db.add(pod)
        db.flush()
        aggregator = Aggregator(pod_id=pod.id, aggregator_uuid=f"e41-{uuid.uuid4().hex[:8]}")
        db.add(aggregator)
        db.flush()
        listener = Listener(
            mac="02:E4:01:00:00:01",
            name="e41-doomed-listener",
            aggregator_id=aggregator.id,
            deployment_id=deployment.id,
        )
        db.add(listener)
        db.flush()

        bundle = _bundle(db, pod)
        kept = {
            "listener": _record(
                db, bundle, pod, aggregator, target_type="listener", target_id=listener.mac
            ),
            "aggregator": _record(
                db,
                bundle,
                pod,
                aggregator,
                target_type="aggregator",
                target_id=aggregator.aggregator_uuid,
            ),  # fmt: skip
        }
        db.commit()
        expected = {
            kind: (r.id, r.target_id, r.pod_id, r.aggregator_uuid, r.file_kind, r.status)
            for kind, r in kept.items()
        }
        pod_id, aggregator_id, mac = pod.id, aggregator.id, listener.mac

    with factory() as db:
        db.execute(delete(Listener).where(Listener.mac == mac))
        db.commit()
        db.execute(delete(Aggregator).where(Aggregator.id == aggregator_id))
        db.execute(delete(Pod).where(Pod.id == pod_id))
        db.commit()

    with factory() as db:
        assert db.get(Listener, mac) is None
        assert db.get(Pod, pod_id) is None
        for _kind, (record_id, *fields) in expected.items():
            record = db.get(DeviceProvisioningRecord, record_id)
            assert record is not None, "a record must outlive the device it describes"
            assert [
                record.target_id,
                record.pod_id,
                record.aggregator_uuid,
                record.file_kind,
                record.status,
            ] == fields


# --- 3. The bundle survives its author ----------------------------------------


def test_deleting_the_generating_user_keeps_the_bundle(factory):
    with factory() as db:
        user = User(email=f"e41-{uuid.uuid4().hex[:6]}@example.com", password_hash="x")
        db.add(user)
        db.flush()
        pod, _, _ = _first_pod(db)
        bundle = _bundle(db, pod, generated_by=user.id)
        db.commit()
        bundle_id, user_id = bundle.id, user.id

    with factory() as db:
        db.execute(delete(User).where(User.id == user_id))
        db.commit()

    with factory() as db:
        bundle = db.get(ProvisioningBundle, bundle_id)
        assert bundle is not None
        assert bundle.generated_by is None
