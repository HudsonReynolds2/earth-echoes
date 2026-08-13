"""Gate 31: E2.1 versioned settings catalog (spec 5.3; DECISIONS D47-D49).

Three-way lock: a hardcoded copy of the spec-5.3 key list pins the CATALOG
constant (drift alarm), the seeded table is asserted equal to the constant
field for field, and the endpoint serves the schema document every role can
read. The constant is asserted - not the runtime table contents against the
spec list - so E2.7's runtime test-key acceptance (inserting a novel row in
its own database) can never red this suite.
"""

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.config.catalog import CATALOG, CATALOG_BY_KEY, CATALOG_VERSION, LEVELS, seed_catalog
from app.main import API_PREFIX, create_app
from app.models import RoleAssignment, SettingsCatalog, User
from app.settings import Settings

# The spec 5.3 table, key for key, copied BY HAND from the spec document.
# If a spec revision adds or removes a key, this list changes with it and
# the constant must follow (and vice versa) - that is the drift alarm.
SPEC_53_KEYS = {
    "audio.sample_rate_hz",
    "audio.bits_per_sample",
    "audio.channels",
    "capture.mode",
    "capture.duty_on_seconds",
    "capture.duty_off_seconds",
    "capture.schedule",
    "listener.wake_grace_seconds",
    "buffering.sd_enabled",
    "buffering.sd_max_bytes",
    "logging.verbosity",
    "network.wifi_ssid",
    "network.wifi_password",
    "network.wifi_security",
    "network.stream_key",
    "network.aggregator_ip",
    "network.stream_endpoint",
    "network.stream_protocol",
    "identity.name",
    "identity.mac",
    "location.gps_lat",
    "location.gps_lon",
    "analysis.model_id",
    "analysis.confidence_threshold",
    "upload.s3_bucket",
    "upload.s3_prefix",
    "upload.s3_endpoint",
    "upload.s3_access_key",
    "upload.s3_secret_key",
    "telemetry.influx_url",
    "telemetry.influx_token",
    "telemetry.influx_database",
    "telemetry.prometheus_url",
    "telemetry.prom_remote_write_url",
    "telemetry.prom_remote_write_user",
    "telemetry.prom_remote_write_password",
    "telemetry.grafana_url",
    # Spec 5.3's thirty-eighth row, added by E5.11 (addendum SPEC-5-01, D134).
    "services.credentials_generation",
}

SECRET_KEYS = {
    "network.wifi_password",
    "network.stream_key",
    "upload.s3_access_key",
    "upload.s3_secret_key",
    "telemetry.influx_token",
    "telemetry.prom_remote_write_password",
}

# D48: the E5 services-onboarding write block - all telemetry.* plus the four
# S3 service/credential keys. upload.s3_prefix is deliberately OUTSIDE it
# (spec 5.1 names it an aggregator-level operator setting; owner ruling).
SERVICE_RESTRICTED_KEYS = {
    "upload.s3_bucket",
    "upload.s3_endpoint",
    "upload.s3_access_key",
    "upload.s3_secret_key",
    "telemetry.influx_url",
    "telemetry.influx_token",
    "telemetry.influx_database",
    "telemetry.prometheus_url",
    "telemetry.prom_remote_write_url",
    "telemetry.prom_remote_write_user",
    "telemetry.prom_remote_write_password",
    "telemetry.grafana_url",
    # E5.11's rotation counter (D134). Write-restricted like the twelve above,
    # but the only one that is not a projection of a service ROW.
    "services.credentials_generation",
}

# D49: inventory-resolved keys read from listener columns, never overrides -
# location.* mandated by E1's INTERFACES contract, identity.* the same
# character (D31/D32).
INVENTORY_KEYS = {"identity.name", "identity.mac", "location.gps_lat", "location.gps_lon"}


# =========================================================================
# The constant against the spec (pure - no database)
# =========================================================================


def test_catalog_matches_spec_53_key_for_key():
    assert len(CATALOG) == 38  # the spec table's row count; duplicates would shrink the set
    assert {entry.key for entry in CATALOG} == SPEC_53_KEYS
    assert {entry.key for entry in CATALOG if entry.secret} == SECRET_KEYS
    assert {
        entry.key for entry in CATALOG if entry.write_restricted is not None
    } == SERVICE_RESTRICTED_KEYS
    assert {entry.key for entry in CATALOG if entry.resolution == "inventory"} == INVENTORY_KEYS


def test_catalog_entries_are_well_formed():
    value_types = {"int", "float", "bool", "string", "object"}
    for entry in CATALOG:
        assert entry.value_type in value_types, entry.key
        assert entry.lowest_level in {*LEVELS, "any"}, entry.key
        assert entry.resolution in {"override", "inventory"}, entry.key
        if entry.enum_values is not None:
            assert entry.default in entry.enum_values, f"{entry.key}: default outside its enum"
        if entry.min_value is not None and entry.max_value is not None:
            assert entry.min_value < entry.max_value, entry.key
        if entry.secret:
            # Secrets ride the string type and can never carry a plaintext
            # default (spec 12.4; the marker mechanics arrive with E2.2).
            assert entry.value_type == "string", entry.key
            assert entry.default is None, entry.key
    assert CATALOG_BY_KEY["capture.schedule"].value_type == "object"
    assert CATALOG_VERSION == 1


def test_spec_level_spot_checks():
    """The lowest_level column drives the E2.2 level rule; pin the five
    level classes with one representative each (spec 5.3)."""
    assert CATALOG_BY_KEY["audio.sample_rate_hz"].lowest_level == "listener"
    assert CATALOG_BY_KEY["listener.wake_grace_seconds"].lowest_level == "aggregator"
    assert CATALOG_BY_KEY["network.wifi_ssid"].lowest_level == "pod"
    assert CATALOG_BY_KEY["telemetry.influx_url"].lowest_level == "deployment"
    assert CATALOG_BY_KEY["logging.verbosity"].lowest_level == "any"
    assert CATALOG_BY_KEY["upload.s3_prefix"].lowest_level == "aggregator"


# =========================================================================
# The seeded table against the constant (integration)
# =========================================================================


@pytest.fixture(scope="module")
def catalog_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate31-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        member = User(email="catalog-member@example.com", password_hash=hash_password(PASSWORD))
        member.role_assignments.append(RoleAssignment(role="viewer", deployment_id=None))
        unassigned = User(
            email="catalog-unassigned@example.com", password_hash=hash_password(PASSWORD)
        )
        db.add_all([member, unassigned])
        db.commit()
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


@pytest.mark.integration
def test_migration_seeded_the_table_field_for_field(catalog_app):
    factory = catalog_app.state.session_factory
    with factory() as db:
        rows = {row.key: row for row in db.scalars(select(SettingsCatalog)).all()}
    assert set(rows) == {entry.key for entry in CATALOG}
    for entry in CATALOG:
        row = rows[entry.key]
        assert row.value_type == entry.value_type, entry.key
        expected_enum = list(entry.enum_values) if entry.enum_values is not None else None
        assert row.enum_values == expected_enum, entry.key
        assert row.min_value == entry.min_value, entry.key
        assert row.max_value == entry.max_value, entry.key
        assert row.default_value == entry.default, entry.key
        assert row.lowest_level == entry.lowest_level, entry.key
        assert row.secret == entry.secret, entry.key
        assert row.resolution == entry.resolution, entry.key
        assert row.write_restricted == entry.write_restricted, entry.key
        assert row.notes == entry.notes, entry.key
        assert row.version == CATALOG_VERSION, entry.key


@pytest.mark.integration
def test_seed_is_idempotent_and_convergent(pg_url):  # noqa: F811
    """Re-running the seed changes nothing, and a row the constant does not
    name is pruned - the property that makes catalog sync migrations safe to
    replay from any point in history (D47)."""
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            seed_catalog(connection)  # second run on an already-seeded table
        with engine.begin() as connection:
            connection.execute(
                SettingsCatalog.__table__.insert().values(
                    key="test.stale_row",
                    value_type="int",
                    lowest_level="listener",
                    secret=False,
                    resolution="override",
                    notes="",
                    version=CATALOG_VERSION,
                )
            )
            seed_catalog(connection)
        with engine.connect() as connection:
            keys = set(connection.scalars(select(SettingsCatalog.key)).all())
    finally:
        engine.dispose()
    assert keys == {entry.key for entry in CATALOG}, "seed must converge on the constant exactly"


# =========================================================================
# The endpoint (integration)
# =========================================================================


@pytest.mark.integration
def test_catalog_endpoint_serves_the_schema_document(catalog_app):
    client = _login(catalog_app, "catalog-member@example.com")
    response = client.get(f"{API_PREFIX}/config/catalog")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == CATALOG_VERSION
    keys = [item["key"] for item in body["items"]]
    assert keys == sorted(keys), "items must sort by key for deterministic rendering"
    assert set(keys) == SPEC_53_KEYS
    by_key = {item["key"]: item for item in body["items"]}
    assert by_key["audio.sample_rate_hz"] == {
        "key": "audio.sample_rate_hz",
        "value_type": "int",
        "enum_values": [8000, 16000, 32000, 48000, 96000, 192000, 250000, 384000],
        "min_value": None,
        "max_value": None,
        "default": 48000,
        "lowest_level": "listener",
        "secret": False,
        "resolution": "override",
        "write_restricted": None,
        "notes": "",
    }
    assert by_key["network.wifi_password"]["secret"] is True
    assert by_key["network.wifi_password"]["default"] is None
    assert by_key["telemetry.influx_url"]["write_restricted"] == "service_onboarding"
    assert by_key["location.gps_lat"]["resolution"] == "inventory"
    assert by_key["analysis.confidence_threshold"]["min_value"] == 0.0
    assert by_key["analysis.confidence_threshold"]["max_value"] == 1.0
    assert by_key["buffering.sd_enabled"]["default"] is True
    assert by_key["upload.s3_prefix"]["default"] == ""


@pytest.mark.integration
def test_catalog_endpoint_requires_an_assignment(catalog_app):
    anonymous = TestClient(catalog_app, raise_server_exceptions=False)
    assert anonymous.get(f"{API_PREFIX}/config/catalog").status_code == 401
    unassigned = _login(catalog_app, "catalog-unassigned@example.com")
    response = unassigned.get(f"{API_PREFIX}/config/catalog")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
