"""E4.3: per-pod file generation (spec 8.2 mode 1; phase-4 E4.3).

The acceptance, and the property each test protects:

1. **The file is byte-identical for every Listener in the Pod.** It is what
   lets a field tech write one file to a stack of cards, so it is asserted
   directly, including against a Listener that has its OWN override (which
   must not leak into a shared file).
2. **The blanks are exactly the catalog's `resolution="inventory"` keys**,
   compared against the catalog's flags rather than a literal list.
3. **A Pod with unset network values fails generation**, naming the missing
   keys, instead of producing a card that cannot join a network.

Plus: no plaintext secret in the bytes (the values come from
`effective_resolved`), and each card's record embeds the device's current
revision without the generator creating one.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from conftest import ephemeral_postgres, make_kek
from sqlalchemy import select

from app.config.catalog import CATALOG
from app.config.overrides import put_overrides
from app.db import create_session_factory
from app.models import Aggregator, ConfigRevision, Deployment, Listener, Pod
from app.provisioning.generate import (
    INVENTORY_KEYS,
    GenerationError,
    generate_pod_file,
    pod_file_path,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy

pytestmark = pytest.mark.integration

PSK = "e43-planted-psk-c81f02-never-on-a-card"
STREAM = "e43-planted-stream-0b77e4-never-on-a-card"

NETWORK = {
    "network.wifi_ssid": "alder-creek-halow",
    "network.wifi_password": PSK,
    "network.wifi_security": "WPA3",
    "network.stream_key": STREAM,
    "network.aggregator_ip": "10.42.0.1",
    "network.stream_endpoint": "10.42.0.1:8765/stream",
    "network.stream_protocol": "ws",
}


@pytest.fixture(scope="module")
def env():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
        yield factory, store


def _pod(db, index: int = 0) -> Pod:
    """The demo Pods only: a Pod a test added without an Aggregator must not
    shift which Pod another test is looking at."""
    return db.scalars(
        select(Pod).join(Aggregator, Aggregator.pod_id == Pod.id).order_by(Pod.name)
    ).all()[index]


def _configured_pod(factory, store, index: int = 0) -> uuid.UUID:
    with factory() as db:
        pod = _pod(db, index)
        put_overrides(db, store, "pod", str(pod.id), NETWORK)
        db.commit()
        return pod.id


def _listeners(db, pod_id) -> list[Listener]:
    aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod_id))
    return list(
        db.scalars(
            select(Listener).where(Listener.aggregator_id == aggregator.id).order_by(Listener.mac)
        )
    )


# --- 1. One file for the whole Pod ---------------------------------------------


def test_the_file_is_byte_identical_for_every_listener_in_the_pod(env):
    factory, store = env
    pod_id = _configured_pod(factory, store, 0)
    with factory() as db:
        listeners = _listeners(db, pod_id)
        assert len(listeners) > 1, "the property needs a Pod with several cards"
        # A Listener-level override must not reach the SHARED file. If the
        # generator resolved per Listener, this card would differ.
        put_overrides(db, store, "listener", listeners[0].mac, {"audio.sample_rate_hz": 96000})
        db.commit()

        first = generate_pod_file(db, store, pod_id)
        second = generate_pod_file(db, store, pod_id)

    assert first.content == second.content
    assert {record.file_path for record in first.records} == {pod_file_path(pod_id)}
    assert {record.target_id for record in first.records} == {
        listener.mac for listener in listeners
    }
    assert yaml.safe_load(first.content)["audio"]["sample_rate_hz"] == 48000


def test_the_blanks_are_exactly_the_catalogs_inventory_keys(env):
    factory, store = env
    pod_id = _configured_pod(factory, store, 0)
    with factory() as db:
        document = yaml.safe_load(generate_pod_file(db, store, pod_id).content)

    blank = {
        f"{group}.{name}"
        for group, members in document.items()
        if isinstance(members, dict)
        for name, value in members.items()
        if value is None
    }
    # `capture.schedule` defaults to null in the catalog and is not a blank
    # left for the cloud, so the comparison is on the inventory keys themselves.
    assert blank & INVENTORY_KEYS == INVENTORY_KEYS
    assert {e.key for e in CATALOG if e.resolution == "inventory"} == INVENTORY_KEYS
    assert blank - INVENTORY_KEYS == {"capture.schedule"}


def test_network_values_reach_the_card_and_secrets_do_not(env):
    factory, store = env
    pod_id = _configured_pod(factory, store, 0)
    with factory() as db:
        content = generate_pod_file(db, store, pod_id).content

    network = yaml.safe_load(content)["network"]
    assert network["wifi_ssid"] == "alder-creek-halow"
    assert network["aggregator_ip"] == "10.42.0.1"
    assert PSK.encode() not in content
    assert STREAM.encode() not in content


# --- 2. A Pod that could not join its network is refused -----------------------


def test_a_pod_with_unset_network_values_is_refused_naming_them(env):
    factory, store = env
    with factory() as db:
        pod = _pod(db, 1)  # the demo seed sets no network values at all
        with pytest.raises(GenerationError) as refused:
            generate_pod_file(db, store, pod.id)

    assert set(refused.value.missing) == {
        "network.wifi_ssid",
        "network.wifi_password",
        "network.stream_key",
        "network.aggregator_ip",
        "network.stream_endpoint",
    }
    for key in refused.value.missing:
        assert key in str(refused.value)


def test_an_open_network_needs_no_password(env):
    factory, store = env
    with factory() as db:
        pod = _pod(db, 2)
        open_network = {k: v for k, v in NETWORK.items() if k != "network.wifi_password"}
        put_overrides(
            db, store, "pod", str(pod.id), open_network | {"network.wifi_security": "OPEN"}
        )
        db.commit()
        generated = generate_pod_file(db, store, pod.id)
    assert yaml.safe_load(generated.content)["network"]["wifi_security"] == "OPEN"


def test_a_pod_without_an_aggregator_is_refused(env):
    factory, store = env
    with factory() as db:
        deployment = db.scalars(select(Deployment)).first()
        lonely = Pod(deployment_id=deployment.id, name="E4.3 pod with no aggregator")
        db.add(lonely)
        db.commit()
        with pytest.raises(GenerationError, match="no aggregator"):
            generate_pod_file(db, store, lonely.id)


# --- 3. Records embed the current revision and never create one ---------------


def test_each_card_records_the_devices_latest_revision_without_creating_one(env):
    factory, store = env
    pod_id = _configured_pod(factory, store, 0)
    with factory() as db:
        listeners = _listeners(db, pod_id)
        with_revisions, without = listeners[0], listeners[1]
        now = datetime.now(UTC)
        older = ConfigRevision(
            target_type="listener",
            target_id=with_revisions.mac,
            deployment_id=with_revisions.deployment_id,
            snapshot={},
            checksum="sha256:" + "1" * 64,
            created_at=now - timedelta(hours=1),
        )
        newer = ConfigRevision(
            target_type="listener",
            target_id=with_revisions.mac,
            deployment_id=with_revisions.deployment_id,
            snapshot={},
            checksum="sha256:" + "2" * 64,
            created_at=now,
        )
        db.add_all([older, newer])
        db.commit()
        before = db.scalar(select(ConfigRevision.id).order_by(ConfigRevision.id).limit(1))
        count_before = len(db.scalars(select(ConfigRevision.id)).all())

        records = {r.target_id: r for r in generate_pod_file(db, store, pod_id).records}

        assert len(db.scalars(select(ConfigRevision.id)).all()) == count_before
        assert before is not None

    assert records[with_revisions.mac].config_revision_id == newer.id
    assert records[with_revisions.mac].config_checksum == newer.checksum
    assert records[without.mac].config_revision_id is None
    assert records[without.mac].config_checksum is None
    assert all(r.file_kind == "pod_file" and r.target_type == "listener" for r in records.values())


def test_excluded_macs_take_no_pod_file_record_and_the_bytes_do_not_change(env):
    """Mixed mode's half of the contract (E4.4 builds on it)."""
    factory, store = env
    pod_id = _configured_pod(factory, store, 0)
    with factory() as db:
        listeners = _listeners(db, pod_id)
        full = generate_pod_file(db, store, pod_id)
        partial = generate_pod_file(db, store, pod_id, exclude_macs=frozenset({listeners[0].mac}))

    assert partial.content == full.content
    assert {r.target_id for r in partial.records} == {listener.mac for listener in listeners[1:]}
