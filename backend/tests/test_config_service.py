"""Gate 33: E2.3 hierarchy walk + merge access (app/config/service.py).

The DB half of the merge engine: ancestry through the E1 foreign keys, the
one-query chain load, the three accessors (redacted / raw / resolved), and
the JSONB round-trip stability case that guards the D52 checksum contract
against driver-level number or encoding drift.
"""

import uuid

import pytest
from conftest import make_kek
from sqlalchemy import select
from test_auth import pg_url  # noqa: F401  (module fixture reuse)

from app.config.canonical import config_checksum
from app.config.overrides import put_overrides
from app.config.service import (
    ancestry,
    effective_for,
    effective_raw,
    effective_resolved,
    override_chain,
)
from app.db import create_session_factory
from app.models import Aggregator, Deployment, EntityOverride, Listener, Organization, Pod
from app.secrets import SecretStore

pytestmark = pytest.mark.integration

MAC_1 = "02:E2:03:00:00:01"
MAC_2 = "02:E2:03:00:00:02"
WIFI_PLAINTEXT = "svc-psk-" + uuid.uuid4().hex


@pytest.fixture(scope="module")
def tree(pg_url):  # noqa: F811
    """org -> deployment -> pod -> aggregator -> two listeners, with
    overrides at org (verbosity), deployment (sample rate), pod (wifi ssid +
    secret password), and listener 1 (sample rate, shadowing deployment)."""
    _, factory = create_session_factory(pg_url)
    store = SecretStore(factory, make_kek())
    with factory() as db:
        org = Organization(name=f"svc-org-{uuid.uuid4().hex[:6]}")
        db.add(org)
        db.flush()
        dep = Deployment(
            organization_id=org.id,
            name=f"svc-dep-{uuid.uuid4().hex[:6]}",
            slug=f"svc-dep-{uuid.uuid4().hex[:6]}",
        )
        db.add(dep)
        db.flush()
        pod = Pod(deployment_id=dep.id, name="svc-pod")
        db.add(pod)
        db.flush()
        agg = Aggregator(pod_id=pod.id, aggregator_uuid=f"svc-agg-{uuid.uuid4().hex[:6]}")
        db.add(agg)
        db.flush()
        db.add_all(
            [
                Listener(mac=MAC_1, name="svc-lst-1", aggregator_id=agg.id, deployment_id=dep.id),
                Listener(
                    mac=MAC_2,
                    name="svc-lst-2",
                    aggregator_id=agg.id,
                    deployment_id=dep.id,
                    gps_lat=47.61,
                    gps_lon=-121.92,
                ),
            ]
        )
        put_overrides(db, store, "organization", str(org.id), {"logging.verbosity": "warn"})
        put_overrides(db, store, "deployment", str(dep.id), {"audio.sample_rate_hz": 96000})
        put_overrides(
            db,
            store,
            "pod",
            str(pod.id),
            {"network.wifi_ssid": "svc-field", "network.wifi_password": WIFI_PLAINTEXT},
        )
        put_overrides(db, store, "listener", MAC_1, {"audio.sample_rate_hz": 192000})
        db.commit()
        ids = {"org": str(org.id), "dep": str(dep.id), "pod": str(pod.id), "agg": str(agg.id)}
    return factory, store, ids


def test_ancestry_resolves_through_the_e1_foreign_keys(tree):
    factory, _, ids = tree
    with factory() as db:
        assert ancestry(db, "organization", ids["org"]) == [("organization", ids["org"])]
        assert ancestry(db, "deployment", ids["dep"]) == [
            ("organization", ids["org"]),
            ("deployment", ids["dep"]),
        ]
        assert ancestry(db, "listener", MAC_1) == [
            ("organization", ids["org"]),
            ("deployment", ids["dep"]),
            ("pod", ids["pod"]),
            ("aggregator", ids["agg"]),
            ("listener", MAC_1),
        ]


def test_ancestry_fails_loud_on_missing_entities(tree):
    factory, _, _ = tree
    with factory() as db:
        with pytest.raises(LookupError):
            ancestry(db, "listener", "02:00:00:00:00:99")
        with pytest.raises(LookupError):
            ancestry(db, "deployment", str(uuid.uuid4()))
        with pytest.raises(ValueError):
            ancestry(db, "fleet", "x")


def test_override_chain_carries_only_levels_with_rows(tree):
    factory, _, ids = tree
    with factory() as db:
        chain = override_chain(db, "listener", MAC_2)
    # Listener 2 has no own row and the aggregator level never got one.
    assert [(link.level, link.entity_id) for link in chain] == [
        ("organization", ids["org"]),
        ("deployment", ids["dep"]),
        ("pod", ids["pod"]),
    ]


def test_effective_for_is_redacted_with_full_provenance(tree):
    factory, _, ids = tree
    with factory() as db:
        config = effective_for(db, "listener", MAC_1)
    assert config["audio.sample_rate_hz"].value == 192000
    assert config["audio.sample_rate_hz"].source == "listener"
    assert config["logging.verbosity"].value == "warn"
    assert config["logging.verbosity"].source == "organization"
    assert config["network.wifi_ssid"].source == "pod"
    assert config["network.wifi_password"].value == {"$secret_set": True}
    assert config["identity.name"].value == "svc-lst-1"
    assert config["identity.name"].source == "inventory"
    assert config["identity.mac"].source_entity_id == MAC_1
    assert WIFI_PLAINTEXT not in repr(config)


def test_sibling_inherits_where_it_does_not_shadow(tree):
    factory, _, _ = tree
    with factory() as db:
        config = effective_for(db, "listener", MAC_2)
    assert config["audio.sample_rate_hz"].value == 96000  # deployment, unshadowed
    assert config["audio.sample_rate_hz"].source == "deployment"
    assert config["location.gps_lat"].value == 47.61
    assert config["location.gps_lon"].value == -121.92


def test_non_listener_levels_omit_inventory_keys(tree):
    factory, _, ids = tree
    with factory() as db:
        config = effective_for(db, "pod", ids["pod"])
    assert "identity.name" not in config
    assert "location.gps_lat" not in config
    assert config["network.wifi_ssid"].value == "svc-field"


def test_raw_carries_the_marker_and_resolved_substitutes_plaintext(tree):
    factory, store, ids = tree
    with factory() as db:
        raw = effective_raw(db, "pod", ids["pod"])
        resolved = effective_resolved(db, "pod", ids["pod"], store)
    marker = raw["network.wifi_password"].value
    assert marker == {"$secret": f"config:pod:{ids['pod']}:network.wifi_password"}
    assert resolved["network.wifi_password"].value == WIFI_PLAINTEXT


def test_snapshot_checksum_survives_a_jsonb_round_trip(tree):
    """The D52 guard against driver drift: a snapshot stored through JSONB
    and reloaded produces the same canonical bytes — floats, non-ASCII, and
    nested objects included."""
    factory, _, _ = tree
    snapshot = {
        "analysis.confidence_threshold": 0.5,
        "tiny": 1e-05,
        "capture.schedule": {"windows": [{"start": "06:00"}]},
        "network.wifi_ssid": "tømmer-skog",
        "buffering.sd_enabled": True,
        "unset": None,
    }
    before = config_checksum(snapshot)
    probe_id = str(uuid.uuid4())
    with factory() as db:
        db.add(
            EntityOverride(
                entity_type="organization",
                entity_id=probe_id,
                overrides=snapshot,
                catalog_version=1,
            )
        )
        db.commit()
    with factory() as db:
        reloaded = db.scalar(
            select(EntityOverride.overrides).where(EntityOverride.entity_id == probe_id)
        )
    assert reloaded == snapshot
    assert config_checksum(reloaded) == before
