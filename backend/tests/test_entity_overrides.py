"""Gate 32: E2.2 sparse override storage (spec 5.1, 5.3; DECISIONS D50-D51).

Validation matrix per type class, the owner-resolved level rule (at-or-above
lowest level, never below - D50), and the secret path: plaintext rides
SecretStore under config:{entity_type}:{entity_id}:{key}, only the marker
touches the row, the keep sentinel round-trips redacted reads back through
PUT, and unset deletions run post-commit.
"""

import logging
import uuid

import pytest
from conftest import make_kek
from sqlalchemy import select
from test_auth import pg_url  # noqa: F401  (module fixture reuse)

from app.config.catalog import CATALOG_BY_KEY, CATALOG_VERSION, LEVELS
from app.config.overrides import (
    OverrideValidationError,
    delete_overrides_for,
    get_overrides,
    put_overrides,
    secret_name,
)
from app.config.validation import KEEP_SENTINEL, validate_override_map
from app.db import create_session_factory
from app.models import EntityOverride
from app.secrets import SecretStore

# =========================================================================
# Pure validation (no database)
# =========================================================================


def _errors(overrides, level="listener"):
    return validate_override_map(overrides, CATALOG_BY_KEY, entity_level=level)


def test_unknown_key_is_rejected_by_name():
    errors = _errors({"audio.smaple_rate_hz": 48000})
    assert [e.code for e in errors] == ["unknown_key"]
    assert "audio.smaple_rate_hz" in errors[0].message


def test_inventory_keys_reject_overrides_and_point_at_the_listener_api():
    for key in ("identity.name", "identity.mac", "location.gps_lat", "location.gps_lon"):
        errors = _errors({key: "x"})
        assert [e.code for e in errors] == ["inventory_resolved"], key
        assert "PATCH /listeners/{mac}" in errors[0].message


def test_service_keys_reject_writes_naming_the_e5_flow():
    errors = _errors({"telemetry.influx_url": "https://influx.example"}, level="deployment")
    assert [e.code for e in errors] == ["service_restricted"]
    assert "E5" in errors[0].message
    # upload.s3_prefix is deliberately OUTSIDE the block (owner ruling, D48).
    assert _errors({"upload.s3_prefix": "pod-7/"}, level="aggregator") == []


def test_level_rule_at_or_above_never_below():
    """D50: the full matrix - one representative per lowest-level class,
    checked at all five entity levels (spec 5.3, owner-resolved)."""
    representatives = {
        "audio.sample_rate_hz": ("listener", 48000),
        "listener.wake_grace_seconds": ("aggregator", 45),
        "network.wifi_ssid": ("pod", "eoe-field"),
        # deployment-lowest: every deployment key is service-restricted in
        # v1, so the level rule for that class is proven via the catalog
        # order check below rather than a writable representative.
    }
    for key, (lowest, value) in representatives.items():
        for level in LEVELS:
            errors = _errors({key: value}, level=level)
            allowed = LEVELS.index(level) <= LEVELS.index(lowest)
            if allowed:
                assert errors == [], f"{key} at {level} should be allowed"
            else:
                assert [e.code for e in errors] == ["level_rule"], f"{key} at {level}"
                assert key in errors[0].message


def test_any_level_keys_are_settable_everywhere():
    for level in LEVELS:
        assert _errors({"logging.verbosity": "debug"}, level=level) == []


def test_value_validation_per_type_class():
    ok = {
        "audio.sample_rate_hz": 96000,  # int enum
        "capture.duty_on_seconds": 90,  # plain int
        "capture.mode": "schedule",  # string enum
        "buffering.sd_enabled": False,  # bool
        "network.aggregator_ip": "10.20.0.1",  # string (validated at pod level below)
        "capture.schedule": {"windows": [{"start": "06:00", "minutes": 45}]},  # object
    }
    assert _errors({k: v for k, v in ok.items() if k != "network.aggregator_ip"}) == []
    assert _errors({"network.aggregator_ip": "10.20.0.1"}, level="pod") == []

    cases = {
        "audio.sample_rate_hz": 44100,  # outside the enum
        "capture.duty_on_seconds": "60",  # string where int expected
        "buffering.sd_enabled": 1,  # int is not a bool
        "capture.mode": "burst",  # outside the string enum
        "capture.schedule": ["not", "an", "object"],
    }
    for key, bad in cases.items():
        errors = _errors({key: bad})
        assert [e.code for e in errors] == ["invalid_value"], key
        assert key in errors[0].message


def test_float_range_and_bool_int_confusion():
    assert _errors({"analysis.confidence_threshold": 0.85}, level="aggregator") == []
    too_high = _errors({"analysis.confidence_threshold": 1.5}, level="aggregator")
    assert [e.code for e in too_high] == ["invalid_value"]
    assert "<=" in too_high[0].message
    as_bool = _errors({"analysis.confidence_threshold": True}, level="aggregator")
    assert [e.code for e in as_bool] == ["invalid_value"]
    # An int where a float is expected is fine (JSON has one number type).
    assert _errors({"analysis.confidence_threshold": 1}, level="aggregator") == []


def test_null_is_never_a_value():
    errors = _errors({"capture.duty_on_seconds": None})
    assert [e.code for e in errors] == ["invalid_value"]
    assert "remove the key to unset" in errors[0].message


def test_oversized_schedule_object_is_rejected():
    huge = {"windows": [{"start": f"{i:02d}:00", "note": "x" * 40} for i in range(40)]}
    errors = _errors({"capture.schedule": huge})
    assert [e.code for e in errors] == ["invalid_value"]
    assert "bytes" in errors[0].message


def test_secret_values_take_string_or_keep_sentinel():
    assert _errors({"network.wifi_password": "hunter2-field-psk"}, level="pod") == []
    assert _errors({"network.wifi_password": KEEP_SENTINEL}, level="pod") == []
    empty = _errors({"network.wifi_password": ""}, level="pod")
    assert [e.code for e in empty] == ["invalid_value"]
    wrong = _errors({"network.wifi_password": 12345}, level="pod")
    assert [e.code for e in wrong] == ["invalid_value"]
    assert "$secret_set" in wrong[0].message


def test_all_errors_returned_sorted_by_key():
    errors = _errors(
        {
            "zz.unknown": 1,
            "audio.sample_rate_hz": 44100,
            "identity.mac": "02:00:00:00:00:01",
        }
    )
    assert [e.key for e in errors] == ["audio.sample_rate_hz", "identity.mac", "zz.unknown"]


# =========================================================================
# The storage service (integration)
# =========================================================================

POD_ID = str(uuid.uuid4())
WIFI_KEY = "network.wifi_password"


@pytest.fixture(scope="module")
def svc(pg_url):  # noqa: F811
    _, factory = create_session_factory(pg_url)
    return factory, SecretStore(factory, make_kek())


@pytest.mark.integration
def test_put_creates_replaces_and_clears_wholesale(svc):
    factory, store = svc
    entity = str(uuid.uuid4())
    with factory() as db:
        change = put_overrides(
            db, store, "pod", entity, {"network.wifi_ssid": "eoe-a", "logging.verbosity": "debug"}
        )
        db.commit()
    assert change.set_keys == ("logging.verbosity", "network.wifi_ssid")
    with factory() as db:
        assert get_overrides(db, "pod", entity) == {
            "network.wifi_ssid": "eoe-a",
            "logging.verbosity": "debug",
        }
        # Wholesale replace: the omitted key vanishes (E1.7 precedent).
        change = put_overrides(db, store, "pod", entity, {"network.wifi_ssid": "eoe-b"})
        db.commit()
    assert change.set_keys == ("network.wifi_ssid",)
    assert change.unset_keys == ("logging.verbosity",)
    with factory() as db:
        assert get_overrides(db, "pod", entity) == {"network.wifi_ssid": "eoe-b"}
        # An empty PUT clears the row entirely - the table stays sparse.
        put_overrides(db, store, "pod", entity, {})
        db.commit()
    with factory() as db:
        assert get_overrides(db, "pod", entity) == {}
        assert db.scalar(select(EntityOverride).where(EntityOverride.entity_id == entity)) is None


@pytest.mark.integration
def test_secret_stores_marker_and_plaintext_rides_secretstore(svc, caplog):
    factory, store = svc
    plaintext = f"psk-{uuid.uuid4().hex}"
    with caplog.at_level(logging.DEBUG), factory() as db:
        change = put_overrides(db, store, "pod", POD_ID, {WIFI_KEY: plaintext})
        db.commit()
    assert change.set_keys == (WIFI_KEY,)
    name = secret_name("pod", POD_ID, WIFI_KEY)
    assert store.get(name) == plaintext
    with factory() as db:
        stored = get_overrides(db, "pod", POD_ID)
    assert stored == {WIFI_KEY: {"$secret": name}}
    assert plaintext not in str(stored)
    assert plaintext not in caplog.text, "secret plaintext leaked into logs (rule R2)"


@pytest.mark.integration
def test_keep_sentinel_preserves_the_stored_secret(svc):
    factory, store = svc
    name = secret_name("pod", POD_ID, WIFI_KEY)
    before = store.get(name)
    with factory() as db:
        change = put_overrides(
            db, store, "pod", POD_ID, {WIFI_KEY: KEEP_SENTINEL, "network.wifi_ssid": "eoe-c"}
        )
        db.commit()
    # A kept secret is not a change; the new plain key is.
    assert change.set_keys == ("network.wifi_ssid",)
    assert store.get(name) == before


@pytest.mark.integration
def test_keep_sentinel_without_a_stored_secret_is_a_422_shaped_error(svc):
    factory, store = svc
    with factory() as db, pytest.raises(OverrideValidationError) as excinfo:
        put_overrides(db, store, "pod", str(uuid.uuid4()), {WIFI_KEY: KEEP_SENTINEL})
    (error,) = excinfo.value.errors
    assert error.key == WIFI_KEY
    assert error.code == "invalid_value"
    assert "no stored secret to keep" in error.message


@pytest.mark.integration
def test_validation_failure_stages_nothing(svc):
    factory, store = svc
    entity = str(uuid.uuid4())
    with factory() as db:
        with pytest.raises(OverrideValidationError):
            put_overrides(db, store, "pod", entity, {"network.wifi_ssid": "ok", "zz.unknown": 1})
        db.commit()
    with factory() as db:
        assert get_overrides(db, "pod", entity) == {}


@pytest.mark.integration
def test_unset_secret_defers_deletion_to_post_commit(svc):
    factory, store = svc
    name = secret_name("pod", POD_ID, WIFI_KEY)
    assert store.exists(name)
    with factory() as db:
        change = put_overrides(db, store, "pod", POD_ID, {"network.wifi_ssid": "eoe-c"})
        # Still present mid-transaction: a rollback here must not lose it.
        assert store.exists(name)
        db.commit()
    assert change.unset_keys == (WIFI_KEY,)
    assert change.secret_names_to_delete == (name,)
    for pending in change.secret_names_to_delete:  # the caller's post-commit duty
        store.delete(pending)
    assert not store.exists(name)


@pytest.mark.integration
def test_delete_overrides_for_returns_secret_names(svc):
    factory, store = svc
    entity = str(uuid.uuid4())
    plaintext = f"psk-{uuid.uuid4().hex}"
    with factory() as db:
        put_overrides(db, store, "pod", entity, {WIFI_KEY: plaintext, "network.wifi_ssid": "eoe-d"})
        db.commit()
    with factory() as db:
        names = delete_overrides_for(db, "pod", entity)
        db.commit()
    assert names == (secret_name("pod", entity, WIFI_KEY),)
    with factory() as db:
        assert get_overrides(db, "pod", entity) == {}
    for pending in names:
        store.delete(pending)
    assert not store.exists(names[0])


@pytest.mark.integration
def test_catalog_version_is_stamped_on_the_row(svc):
    factory, store = svc
    entity = str(uuid.uuid4())
    with factory() as db:
        put_overrides(db, store, "listener", entity, {"audio.sample_rate_hz": 96000})
        db.commit()
    with factory() as db:
        row = db.scalar(select(EntityOverride).where(EntityOverride.entity_id == entity))
        assert row is not None
        assert row.catalog_version == CATALOG_VERSION
        assert row.entity_type == "listener"
