"""E5.7a: the projection, the privileged write, and the `changed_keys` fix.

The four acceptance criteria from the phase document, and why each is shaped
the way it is:

1. **`set(service_settings(...)) ⊆ the catalog's write_restricted keys**,
   asserted against `CATALOG` itself and never a copied list — a copy is what
   would let the projection quietly start writing a key operators are also
   allowed to write.

2. **`PUT .../config/overrides` STILL 422s with `service_restricted`** for
   every one of the twelve. This is the test that proves the flag's default
   rather than assuming it, and it is why E5 gated the check instead of
   deleting it.

3. **One revision per Aggregator, thirty `no_op` Listener plans**, with the
   listeners' snapshots byte-identical before and after. This is the pin on
   the E2-owned `changed_keys` defect: before the fix, one services save
   minted a revision for every Listener in the deployment whose published
   bytes were identical to the previous one. It is written as an acceptance
   test precisely so it cannot later be dropped as "an optimization".

4. **Saving twice with identical input creates zero new revisions**, which is
   the same property from the other side.
"""

import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.config.catalog import CATALOG, CATALOG_BY_KEY
from app.config.overrides import get_overrides, put_overrides
from app.config.plan import restricted_keys
from app.config.validation import validate_override_map
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    ConfigRevision,
    Deployment,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.services.projection import PROJECTION, RESTRICTED_KEYS, service_settings
from app.services.store import load_services
from app.settings import Settings

SLUG = "e57a"
OWNER_EMAIL = "e57a-owner@example.com"
PASSWORD = "e57a-owner-password"
LISTENERS = 30

#: The twelve, written out ONCE here so the tests below can be read without
#: re-deriving them — and immediately asserted equal to what the catalog says,
#: so this constant cannot become the second source of truth it looks like.
TWELVE = {
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
}


def full_body() -> dict:
    """All four projecting services configured, so the projection is complete."""
    return {
        "services": {
            "influx": {
                "url": "https://influx.example:8181",
                "database": "recordings",
                "token": "influx-token-value",
            },
            "prometheus": {
                "read_url": "https://prom.example:9090",
                "remote_write_url": "https://prom.example:9090/api/v1/write",
                "remote_write_user": "eoe",
                "remote_write_password": "prom-password-value",
            },
            "grafana": {
                "base_url": "https://grafana.example:3000",
                "service_account_token": "grafana-token-value",
            },
            "s3": {
                "bucket": "eoe-audio",
                "region": "us-east-1",
                "endpoint": "https://minio.example:9000",
                "access_key": "s3-access-value",
                "secret_key": "s3-secret-value",
            },
        }
    }


# --- The projection is a function of the catalog, not of a copied list -------


def test_the_catalog_is_the_source_of_the_restricted_set() -> None:
    assert RESTRICTED_KEYS == TWELVE
    assert restricted_keys() == TWELVE
    assert {entry.key for entry in CATALOG if entry.write_restricted is not None} == TWELVE


def test_every_projected_key_is_a_write_restricted_catalog_key() -> None:
    """The subset property from the acceptance, against `CATALOG` itself."""
    projected = {key for _, _, key, _ in PROJECTION}
    assert projected <= {entry.key for entry in CATALOG if entry.write_restricted is not None}


def test_the_projection_marks_exactly_the_catalog_secrets_as_secret() -> None:
    """A field the projection reads from `config` when the catalog calls it a
    secret would put a plaintext credential in a non-secret override."""
    for _, _, catalog_key, is_secret in PROJECTION:
        assert CATALOG_BY_KEY[catalog_key].secret == is_secret, catalog_key


def test_the_mqtt_row_projects_nothing() -> None:
    """Broker coordinates reach a device through E4.6's bootstrap block, not
    through retained desired config — a device that could only learn its broker
    address over the broker could never make the first connection."""
    assert not [entry for entry in PROJECTION if entry[0] == "mqtt"]


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def app(pg_url):
    """One deployment, one Pod, one Aggregator, thirty Listeners.

    The shape the acceptance names, built once: the assertion is about what a
    single services save does to thirty Listeners, and thirty is not a round
    number chosen for looks — it is the SIM fleet's per-Aggregator count, which
    is where ~600 pointless revisions per save came from.
    """
    application = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e57a-test-secret",
            kek=make_kek(),
            cors_origins="",
            publish_enabled=False,
        )
    )
    _, factory = create_session_factory(pg_url)
    with factory() as db:
        org = Organization(name="e57a-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E57a", slug=SLUG)
        db.add(dep)
        db.flush()
        pod = Pod(deployment_id=dep.id, name="e57a-pod")
        db.add(pod)
        db.flush()
        aggregator = Aggregator(pod_id=pod.id, aggregator_uuid="e57a-agg", name="e57a-agg")
        db.add(aggregator)
        db.flush()
        for index in range(LISTENERS):
            db.add(
                Listener(
                    mac=f"02:E5:7A:00:00:{index:02X}",
                    aggregator_id=aggregator.id,
                    deployment_id=dep.id,
                    name=f"e57a-listener-{index:02d}",
                )
            )
        user = User(email=OWNER_EMAIL, password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role=Role.OWNER.value, deployment_id=None))
        db.add(user)
        db.commit()
        application.state.e57a_deployment_id = dep.id
        application.state.e57a_aggregator_id = aggregator.id
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e57a_deployment_id


@pytest.fixture
def owner(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


@pytest.fixture(autouse=True)
def clean(app, dep_id):
    """Every test starts with no services, no overrides and no revisions."""
    from sqlalchemy import delete as sql_delete

    from app.models import EntityOverride

    def wipe() -> None:
        with app.state.session_factory() as db:
            for row in load_services(db, dep_id):
                db.delete(row)
            db.execute(sql_delete(ConfigRevision))
            db.execute(sql_delete(EntityOverride))
            db.commit()

    wipe()
    yield
    wipe()


def revisions(app) -> list[ConfigRevision]:
    with app.state.session_factory() as db:
        return list(db.query(ConfigRevision).all())


# --- The refusal that proves the flag's default -------------------------------


def test_config_overrides_still_refuses_every_one_of_the_twelve(owner, dep_id) -> None:
    """**The acceptance that keeps the shortcut honest.**

    E5 gated the `service_restricted` check behind a keyword-only flag instead
    of deleting it. This is what proves the flag defaults off on the operator
    path, one key at a time so a failure names the key that leaked.
    """
    for key in sorted(TWELVE):
        response = owner.put(
            f"{API_PREFIX}/deployments/{dep_id}/config/overrides",
            json={"overrides": {key: "anything"}},
            headers=csrf(owner),
        )
        assert response.status_code == 422, f"{key}: {response.text}"
        codes = {error["code"] for error in response.json()["error"]["detail"]["errors"]}
        assert codes == {"service_restricted"}, f"{key}: {response.text}"


def test_the_pure_validator_refuses_them_by_default_and_permits_them_on_request() -> None:
    """The same property one level down, where the flag actually lives."""
    changes = dict.fromkeys(TWELVE, "value")
    refused = validate_override_map(changes, CATALOG_BY_KEY, entity_level="deployment")
    assert {error.code for error in refused} == {"service_restricted"}
    assert {error.key for error in refused} == TWELVE

    permitted = validate_override_map(
        changes, CATALOG_BY_KEY, entity_level="deployment", allow_write_restricted=True
    )
    assert permitted == []


def test_the_flag_does_not_switch_off_the_other_rules() -> None:
    """It permits these keys; it does not make validation lenient.

    An unknown key, an inventory-resolved key and a level violation all still
    fail with the flag on — otherwise `allow_write_restricted` would be a
    "skip validation" switch wearing a narrower name.
    """
    errors = validate_override_map(
        {
            "not.a.real.key": 1,
            "identity.mac": "AA:BB:CC:DD:EE:FF",
            "telemetry.influx_url": "https://influx.example",
        },
        CATALOG_BY_KEY,
        entity_level="deployment",
        allow_write_restricted=True,
    )
    assert {error.key: error.code for error in errors} == {
        "not.a.real.key": "unknown_key",
        "identity.mac": "inventory_resolved",
    }


def test_put_overrides_refuses_a_restricted_key_by_default(app, dep_id) -> None:
    """`put_overrides` is the only writer of `entity_override`, so its own
    default is what actually protects the row."""
    from app.config.overrides import OverrideValidationError

    with app.state.session_factory() as db:
        with pytest.raises(OverrideValidationError) as caught:
            put_overrides(
                db,
                app.state.secret_store,
                "deployment",
                str(dep_id),
                {"telemetry.influx_url": "https://influx.example"},
            )
        assert [error.code for error in caught.value.errors] == ["service_restricted"]


# --- The projection over real rows --------------------------------------------


def test_a_full_save_covers_exactly_the_twelve_keys(owner, dep_id, app) -> None:
    response = owner.put(
        f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner)
    )
    assert response.status_code == 200, response.text

    with app.state.session_factory() as db:
        projected = service_settings(load_services(db, dep_id), app.state.secret_store.get)
    assert set(projected) == TWELVE


def test_the_projection_is_a_subset_of_the_restricted_keys_when_partial(owner, dep_id, app) -> None:
    """Two services configured: a strict subset, and never a stray key."""
    body = {"services": {"grafana": full_body()["services"]["grafana"]}}
    assert (
        owner.put(
            f"{API_PREFIX}/deployments/{dep_id}/services", json=body, headers=csrf(owner)
        ).status_code
        == 200
    )
    with app.state.session_factory() as db:
        projected = service_settings(load_services(db, dep_id), app.state.secret_store.get)
    assert set(projected) == {"telemetry.grafana_url"}
    assert set(projected) < TWELVE


def test_a_cleared_optional_field_leaves_the_projection(owner, dep_id, app) -> None:
    """**Wholesale regeneration, not a merge.**

    The S3 endpoint is optional. Saving S3 again without it must remove
    `upload.s3_endpoint` from the deployment's overrides rather than leaving
    the old value there forever — which is what a merge would do, and what
    would eventually deliver a stale endpoint to a device.
    """
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    with app.state.session_factory() as db:
        assert "upload.s3_endpoint" in get_overrides(db, "deployment", str(dep_id))

    without = full_body()
    without["services"] = {"s3": dict(without["services"]["s3"])}
    del without["services"]["s3"]["endpoint"]
    without["services"]["s3"]["access_key"] = {"$secret_set": True}
    without["services"]["s3"]["secret_key"] = {"$secret_set": True}
    response = owner.put(
        f"{API_PREFIX}/deployments/{dep_id}/services", json=without, headers=csrf(owner)
    )
    assert response.status_code == 200, response.text

    with app.state.session_factory() as db:
        overrides = get_overrides(db, "deployment", str(dep_id))
    assert "upload.s3_endpoint" not in overrides
    # The other services' keys survived: regeneration replaces the RESTRICTED
    # block, and every service still configured is still in it.
    assert "telemetry.grafana_url" in overrides


def test_an_unrelated_operator_override_survives_a_services_save(owner, dep_id, app) -> None:
    """The regeneration replaces the twelve and touches nothing else."""
    with app.state.session_factory() as db:
        put_overrides(
            db, app.state.secret_store, "deployment", str(dep_id), {"logging.verbosity": "debug"}
        )
        db.commit()

    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))

    with app.state.session_factory() as db:
        overrides = get_overrides(db, "deployment", str(dep_id))
    assert overrides["logging.verbosity"] == "debug"
    assert "telemetry.influx_url" in overrides


def test_no_secret_plaintext_reaches_the_override_row(owner, dep_id, app) -> None:
    """D51: the row holds markers. The duplicated ciphertext is deliberate
    (fixed choice 3); the duplicated PLAINTEXT would be the defect."""
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    with app.state.session_factory() as db:
        overrides = get_overrides(db, "deployment", str(dep_id))
    for key in ("telemetry.influx_token", "upload.s3_access_key"):
        assert set(overrides[key]) == {"$secret"}, overrides[key]
    rendered = repr(overrides)
    for plaintext in ("influx-token-value", "s3-access-value", "prom-password-value"):
        assert plaintext not in rendered


def test_an_unreadable_credential_does_not_stop_the_other_eleven_keys(app, dep_id, owner) -> None:
    """One broken secret degrades one key, not the whole save (the D64 rule)."""
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))

    def broken(name: str) -> str:
        if name.endswith("influx_token"):
            raise RuntimeError("ciphertext gone")
        return app.state.secret_store.get(name)

    with app.state.session_factory() as db:
        projected = service_settings(load_services(db, dep_id), broken)
    assert "telemetry.influx_token" not in projected
    assert set(projected) == TWELVE - {"telemetry.influx_token"}


# --- The changed_keys fix, as the acceptance words it -------------------------


def test_one_save_makes_one_revision_for_the_aggregator_and_none_for_listeners(
    owner, dep_id, app
) -> None:
    """**The pin on the E2-owned defect.**

    Spec 5.4 keeps the twelve keys off Listener-bound config, so a Listener's
    published snapshot is byte-identical before and after a services save.
    Before the fix, `changed_keys` compared the RAW effective maps — which
    include those keys — so every Listener was "changed", got a revision, and
    got a retained publish carrying bytes it already had. Thirty here; ~600 on
    a SIM fleet, per save.
    """
    response = owner.put(
        f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner)
    )
    assert response.status_code == 200, response.text
    assert response.json() is not None

    minted = revisions(app)
    assert [row.target_type for row in minted] == ["aggregator"], (
        f"{len(minted)} revisions, targets "
        f"{sorted({row.target_type for row in minted})} — a services save must reach "
        "Aggregators only"
    )
    assert len(minted) == 1


def test_the_listener_snapshots_are_byte_identical_across_a_services_save(
    owner, dep_id, app
) -> None:
    """The other half of the same acceptance, stated about the BYTES.

    Asserted through the same `snapshot_from_raw` the publisher uses, so this
    proves what a device would have received rather than what a plan claimed.
    """
    from app.config.plan import snapshot_from_raw
    from app.config.service import effective_raw

    def listener_snapshots() -> dict[str, dict]:
        with app.state.session_factory() as db:
            macs = [row.mac for row in db.query(Listener).all()]
            return {
                mac: snapshot_from_raw("listener", effective_raw(db, "listener", mac))
                for mac in macs
            }

    before = listener_snapshots()
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    after = listener_snapshots()
    assert len(before) == LISTENERS
    assert before == after


def test_saving_twice_with_identical_input_creates_zero_new_revisions(owner, dep_id, app) -> None:
    """No-op by construction: the second save projects the same twelve values,
    the Aggregator's snapshot does not change, and no revision is minted."""
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    first = {row.id for row in revisions(app)}
    assert len(first) == 1

    body = full_body()
    for service, secret_fields in (
        ("influx", ("token",)),
        ("prometheus", ("remote_write_password",)),
        ("grafana", ("service_account_token",)),
        ("s3", ("access_key", "secret_key")),
    ):
        for field in secret_fields:
            body["services"][service][field] = {"$secret_set": True}

    response = owner.put(
        f"{API_PREFIX}/deployments/{dep_id}/services", json=body, headers=csrf(owner)
    )
    assert response.status_code == 200, response.text
    assert {row.id for row in revisions(app)} == first


def test_the_aggregator_revision_carries_all_twelve_keys(owner, dep_id, app) -> None:
    """The point of the whole unit: the device is actually told."""
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    minted = revisions(app)
    assert len(minted) == 1
    assert set(minted[0].snapshot) >= TWELVE


def test_the_audit_entry_counts_revisions_and_names_no_value(owner, dep_id, app) -> None:
    from app.models import AuditLog

    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    with app.state.session_factory() as db:
        entry = (
            db.query(AuditLog)
            .filter(AuditLog.action == "services.update", AuditLog.entity_id == str(dep_id))
            .order_by(AuditLog.at.desc())
            .first()
        )
    assert entry is not None
    assert entry.detail["revisions"] == 1
    rendered = repr(entry.detail)
    for plaintext in ("influx-token-value", "s3-access-value", "https://influx.example:8181"):
        assert plaintext not in rendered


def test_effective_config_shows_the_twelve_at_deployment_source_and_redacted(
    owner, dep_id, app
) -> None:
    """The acceptance's `GET .../config/effective` half."""
    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    response = owner.get(
        f"{API_PREFIX}/aggregators/{app.state.e57a_aggregator_id}/config/effective"
    )
    assert response.status_code == 200, response.text
    values = response.json()["config"]
    for key in TWELVE:
        assert values[key]["source"] == "deployment", (key, values[key])
    for key, plaintext in (
        ("telemetry.influx_token", "influx-token-value"),
        ("upload.s3_access_key", "s3-access-value"),
        ("upload.s3_secret_key", "s3-secret-value"),
        ("telemetry.prom_remote_write_password", "prom-password-value"),
    ):
        assert values[key]["value"] != plaintext, key


def test_effective_resolved_hands_the_aggregator_the_real_credentials(owner, dep_id, app) -> None:
    """Redacted for an operator, resolved for the device (the other half)."""
    from app.config.service import effective_resolved

    owner.put(f"{API_PREFIX}/deployments/{dep_id}/services", json=full_body(), headers=csrf(owner))
    with app.state.session_factory() as db:
        resolved = effective_resolved(
            db, "aggregator", str(app.state.e57a_aggregator_id), app.state.secret_store
        )
    assert resolved["telemetry.influx_token"].value == "influx-token-value"
    assert resolved["upload.s3_access_key"].value == "s3-access-value"
