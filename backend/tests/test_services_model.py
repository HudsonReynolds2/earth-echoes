"""E5.1: the widened `deployment_service` table and its store (spec 16.2, 16.5).

Four things are pinned here, and each exists because losing it silently would
be expensive:

1. **The five service keys are a transcription of the spec 16.2 table**, the
   way `test_settings_catalog.py` pins CATALOG against spec 5.3. A sixth key
   added without a spec change is a red gate, not a merge.
2. **The conditional requirement is the DATABASE's**, not Python's. A Grafana
   row with no host inserts; an `mqtt` row with no host raises IntegrityError.
   A Python-side guard would be routed around by the first writer that forgot
   it, and `load_broker_coordinates` is the function every deployment's
   control plane depends on.
3. **`load_broker_coordinates` returns exactly what it returned before the
   migration**, asserted by seeding through `devbroker.register_services`,
   reading the rows at the PREVIOUS revision the way E3's loader read them,
   and comparing to what the loader returns at head.
4. **`DELETE /deployments/{id}` works**, which before this task it did not:
   `deployment_service.deployment_id` is a real foreign key and every
   deployment has an `mqtt` row, so the endpoint 500'd on any real deployment.

Credential values never appear here; the rows name SecretStore entries and
the store holds the plaintext (rule R2).
"""

import logging
import subprocess
import sys
import uuid

import pytest
from conftest import REPO_ROOT, docker_env, ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.auth.passwords import hash_password
from app.controlplane.broker import BrokerCoordinates, load_broker_coordinates
from app.db import create_session_factory
from app.devbroker import plan_accounts, register_services
from app.devbroker import secret_name as broker_secret_name
from app.main import API_PREFIX, create_app
from app.models import (
    SERVICE_KEYS,
    SERVICE_STATUS_VOCAB,
    SERVICES_STATUS_VOCAB,
    Deployment,
    DeploymentService,
    Organization,
    RoleAssignment,
    Secret,
    User,
)
from app.secrets import SecretStore
from app.seed import seed_demo_hierarchy
from app.services.store import (
    delete_services_for,
    load_service,
    load_services,
    upsert_service,
)
from app.settings import Settings

pytestmark = pytest.mark.integration

BACKEND = REPO_ROOT / "backend"
OWNER = "e51-owner@example.com"
PASSWORD = "correct horse battery staple"  # noqa: S105  (test account, not a credential)

#: A stand-in trust anchor. Deliberately a CERTIFICATE header and never a
#: private key one, so `test_repo_layout.SECRET_PATTERNS` stays quiet.
FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

#: **Spec 16.2's table, transcribed by hand**: its Service column, mapped to
#: the `service_key` the platform stores each one under. If a spec revision
#: adds, removes or renames a service, this mapping changes with it and
#: `models.SERVICE_KEYS` must follow (and vice versa) - that is the alarm.
SPEC_16_2_SERVICES: dict[str, str] = {
    "mqtt": "Mosquitto",
    "influx": "InfluxDB 3",
    "prometheus": "Prometheus",
    "grafana": "Grafana",
    "s3": "Object storage (S3)",
}

#: Spec 16.2's "Each service carries a per-service status" sentence, and spec
#: 16.5's rolled-up vocabulary. **Two vocabularies, deliberately distinct** -
#: no single service can say "this deployment is degraded".
SPEC_16_2_SERVICE_STATUSES = ("untested", "verified", "failed")
SPEC_16_5_ROLLUP_STATUSES = ("unconfigured", "pending_verification", "verified", "degraded")


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def factory(pg_url):
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def org_id(factory):
    with factory() as db:
        org = Organization(name="e51-org")
        db.add(org)
        db.commit()
        return org.id


def _deployment(factory, org_id, slug: str) -> uuid.UUID:
    with factory() as db:
        row = Deployment(organization_id=org_id, name=slug.replace("-", " "), slug=slug)
        db.add(row)
        db.commit()
        return row.id


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={**docker_env(), "DATABASE_URL": url},
        timeout=120,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stdout}\n{result.stderr}"


# --- 1. The vocabulary is the spec's, not the code's -------------------------


def test_service_keys_are_the_spec_16_2_table():
    assert tuple(SPEC_16_2_SERVICES) == SERVICE_KEYS, (
        "deployment_service.service_key no longer matches the spec 16.2 service list. "
        "A sixth service needs a spec revision first (rule R2, phase-5 fixed choice 1)."
    )


def test_the_two_status_vocabularies_are_the_spec_s_and_are_distinct():
    assert SERVICE_STATUS_VOCAB == SPEC_16_2_SERVICE_STATUSES
    assert SERVICES_STATUS_VOCAB == SPEC_16_5_ROLLUP_STATUSES
    assert set(SERVICE_STATUS_VOCAB) != set(SERVICES_STATUS_VOCAB)


def test_the_database_accepts_every_spec_key_and_refuses_a_sixth(factory, org_id):
    dep_id = _deployment(factory, org_id, "vocab-accepts")
    with factory() as db:
        for key in SERVICE_KEYS:
            upsert_service(
                db,
                dep_id,
                key,
                host="broker.example" if key == "mqtt" else None,
                port=8883 if key == "mqtt" else None,
                username="platform" if key == "mqtt" else None,
                password_secret_name=broker_secret_name(dep_id) if key == "mqtt" else None,
            )
        db.commit()
        assert {row.service_key for row in load_services(db, dep_id)} == set(SERVICE_KEYS)

    # The DB, not the store's ValueError, is what makes a sixth key impossible.
    with factory() as db, pytest.raises(IntegrityError) as raised:
        db.add(DeploymentService(deployment_id=dep_id, service_key="loki"))
        db.flush()
    assert "service_key_vocab" in str(raised.value)


# --- 2. The conditional requirement is enforced by the database --------------


def test_a_non_mqtt_row_needs_no_host_or_port(factory, org_id):
    dep_id = _deployment(factory, org_id, "conditional-ok")
    with factory() as db:
        row = upsert_service(
            db,
            dep_id,
            "grafana",
            config={"base_url": "https://grafana.example"},
            secret_names={"service_account_token": f"deployment:{dep_id}:grafana_token"},
        )
        db.commit()
        db.refresh(row)
        assert (row.host, row.port, row.username, row.password_secret_name) == (
            None,
            None,
            None,
            None,
        )
        assert row.config == {"base_url": "https://grafana.example"}


def test_the_database_refuses_an_mqtt_row_without_a_host(factory, org_id):
    """Rejected by the CHECK, not by Python - a guard in the store would be
    routed around by the first writer that forgot to call it, and the broker
    row is what every deployment's control plane is dialled from."""
    dep_id = _deployment(factory, org_id, "conditional-bad")
    with factory() as db, pytest.raises(IntegrityError) as raised:
        db.add(
            DeploymentService(
                deployment_id=dep_id,
                service_key="mqtt",
                host=None,
                port=8883,
                username="platform",
                password_secret_name=broker_secret_name(dep_id),
            )
        )
        db.flush()
    assert "mqtt_coordinates_required" in str(raised.value)


def test_status_columns_default_to_untested_and_the_vocabulary_is_enforced(factory, org_id):
    dep_id = _deployment(factory, org_id, "status-defaults")
    with factory() as db:
        row = upsert_service(db, dep_id, "s3", config={"bucket": "audio"})
        db.commit()
        db.refresh(row)
        assert row.status == "untested"
        assert row.consecutive_failures == 0
        assert row.status_reason is None
        assert row.last_tested_at is None
        assert row.last_test_detail is None
        assert row.secret_names == {}

    with factory() as db, pytest.raises(IntegrityError) as raised:
        db.execute(
            text("UPDATE deployment_service SET status = 'flapping' WHERE deployment_id = :d"),
            {"d": dep_id},
        )
    assert "status_vocab" in str(raised.value)


def test_deployment_services_status_defaults_to_unconfigured_and_is_constrained(factory, org_id):
    dep_id = _deployment(factory, org_id, "rollup-default")
    with factory() as db:
        assert db.get(Deployment, dep_id).services_status == "unconfigured"

    # E5.5's roll_up is the only writer; the column still refuses a value
    # outside spec 16.5's four, whoever writes it.
    with factory() as db, pytest.raises(IntegrityError) as raised:
        db.execute(
            text("UPDATE deployment SET services_status = 'mostly_fine' WHERE id = :d"),
            {"d": dep_id},
        )
    assert "services_status_vocab" in str(raised.value)


# --- 3. The E3 loader is untouched by the widening ---------------------------


def _pre_migration_coordinates(factory, store: SecretStore) -> list[BrokerCoordinates]:
    """`load_broker_coordinates` as it stood BEFORE revision a31287354e23,
    reading only the columns that existed then, in raw SQL so it can run
    against the downgraded schema where the ORM model does not fit."""
    coordinates: list[BrokerCoordinates] = []
    with factory() as db:
        rows = db.execute(
            text(
                "SELECT s.deployment_id, d.slug, s.host, s.port, s.username, "
                "s.password_secret_name, s.tls_enabled, s.ca_cert_pem "
                "FROM deployment_service s "
                "JOIN deployment d ON d.id = s.deployment_id "
                "WHERE s.service_key = 'mqtt' ORDER BY d.slug"
            )
        ).all()
    for row in rows:
        coordinates.append(
            BrokerCoordinates(
                deployment_id=row.deployment_id,
                slug=row.slug,
                host=row.host,
                port=row.port,
                username=row.username,
                password=store.get(row.password_secret_name),
                tls_enabled=row.tls_enabled,
                ca_cert_pem=row.ca_cert_pem,
            )
        )
    return coordinates


def test_broker_coordinates_are_identical_before_and_after_the_migration():
    """Seeded through `devbroker.register_services`, exactly as the dev broker
    generator seeds them, then compared across the migration boundary: read at
    the previous revision by the pre-E5.1 loader body, and at head by the real
    `load_broker_coordinates`. `BrokerCoordinates` is a frozen dataclass, so
    equality covers the password too."""
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        store = SecretStore(factory, make_kek())
        with factory() as db:
            seed_demo_hierarchy(db)
            db.commit()
            accounts = plan_accounts(db)
            written = register_services(
                db, store, accounts, host="localhost", port=8883, ca_cert_pem=FAKE_CA_PEM
            )
        assert written >= 2

        at_head = load_broker_coordinates(factory, store)
        assert at_head, "the seeded broker rows produced no coordinates"

        _alembic(url, "downgrade", "-1")
        before = _pre_migration_coordinates(factory, store)
        _alembic(url, "upgrade", "head")
        after = load_broker_coordinates(factory, store)

        assert before == at_head
        assert after == at_head


def test_a_broker_row_missing_connection_details_is_skipped_not_fatal(factory, org_id, caplog):
    """D64's rule, extended to the columns E5.1 made nullable: one badly
    provisioned deployment must not deafen the others. The CHECK makes this
    row impossible today, so the test suspends the CHECK to reach the branch -
    which is the point, because the branch exists for a future migration that
    changes that constraint."""
    good_id = _deployment(factory, org_id, "skip-good")
    bad_id = _deployment(factory, org_id, "skip-bad")
    store = SecretStore(factory, make_kek())
    store.put(broker_secret_name(good_id), "good-pw")
    store.put(broker_secret_name(bad_id), "bad-pw")
    with factory() as db:
        for dep_id in (good_id, bad_id):
            upsert_service(
                db,
                dep_id,
                "mqtt",
                host="broker.example",
                port=8883,
                username="platform",
                password_secret_name=broker_secret_name(dep_id),
            )
        db.commit()

    constraint = "ck_deployment_service_mqtt_coordinates_required"
    with factory() as db:
        db.execute(text(f"ALTER TABLE deployment_service DROP CONSTRAINT {constraint}"))
        db.execute(
            text("UPDATE deployment_service SET host = NULL WHERE deployment_id = :d"),
            {"d": bad_id},
        )
        db.commit()
    try:
        with caplog.at_level(logging.WARNING, logger="app.controlplane.broker"):
            coordinates = load_broker_coordinates(factory, store)
        slugs = {coords.slug for coords in coordinates}
        assert "skip-good" in slugs, "one bad row deafened a healthy deployment"
        assert "skip-bad" not in slugs
        assert "skip-bad" in caplog.text and "host" in caplog.text
        assert "bad-pw" not in caplog.text, "the warning must never name a credential"
    finally:
        with factory() as db:
            db.execute(
                sql_delete(DeploymentService).where(DeploymentService.deployment_id == bad_id)
            )
            db.commit()
            db.execute(
                text(
                    f"ALTER TABLE deployment_service ADD CONSTRAINT {constraint} CHECK ("
                    "service_key <> 'mqtt' OR (host IS NOT NULL AND port IS NOT NULL "
                    "AND username IS NOT NULL AND password_secret_name IS NOT NULL))"
                )
            )
            db.commit()


# --- The store ---------------------------------------------------------------


def test_upsert_replaces_wholesale_and_load_services_follows_the_spec_order(factory, org_id):
    dep_id = _deployment(factory, org_id, "store-upsert")
    with factory() as db:
        upsert_service(db, dep_id, "s3", config={"bucket": "one", "region": "us-east-1"})
        upsert_service(db, dep_id, "influx", config={"database": "birds"})
        db.commit()

    with factory() as db:
        # Second write of the same key updates rather than duplicating, and
        # drops the field the caller omitted (PUT is never a merge).
        upsert_service(db, dep_id, "s3", config={"bucket": "two"})
        db.commit()

    with factory() as db:
        rows = load_services(db, dep_id)
        assert [row.service_key for row in rows] == ["influx", "s3"]  # spec 16.2 order
        assert load_service(db, dep_id, "s3").config == {"bucket": "two"}
        assert load_service(db, dep_id, "grafana") is None


def test_upsert_refuses_a_key_outside_the_vocabulary(factory, org_id):
    dep_id = _deployment(factory, org_id, "store-badkey")
    with factory() as db, pytest.raises(ValueError, match="loki"):
        upsert_service(db, dep_id, "loki")


def test_delete_services_for_returns_every_secret_name_it_orphaned(factory, org_id):
    dep_id = _deployment(factory, org_id, "store-delete")
    with factory() as db:
        upsert_service(
            db,
            dep_id,
            "mqtt",
            host="broker.example",
            port=8883,
            username="platform",
            password_secret_name=broker_secret_name(dep_id),
        )
        upsert_service(
            db,
            dep_id,
            "s3",
            config={"bucket": "audio"},
            secret_names={
                "access_key": f"deployment:{dep_id}:s3_access_key",
                "secret_key": f"deployment:{dep_id}:s3_secret_key",
            },
        )
        db.commit()

    with factory() as db:
        orphaned = delete_services_for(db, dep_id)
        db.commit()

    assert set(orphaned) == {
        broker_secret_name(dep_id),
        f"deployment:{dep_id}:s3_access_key",
        f"deployment:{dep_id}:s3_secret_key",
    }
    with factory() as db:
        assert load_services(db, dep_id) == []


# --- 4. DELETE /deployments/{id} ---------------------------------------------


@pytest.fixture(scope="module")
def delete_app(pg_url, factory, org_id):
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e51-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with factory() as db:
        user = User(email=OWNER, password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        db.add(user)
        db.commit()
    return app


def _owner(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def _seed_five_services(app, dep_id: uuid.UUID) -> list[str]:
    """All five spec 16.2 rows plus a SecretStore entry behind each named
    credential. Values are short and meaningless; only the NAMES live in the
    rows (rule R2)."""
    names = {
        "mqtt": {"password_secret_name": broker_secret_name(dep_id)},
        "influx": {"secret_names": {"token": f"deployment:{dep_id}:influx_token"}},
        "prometheus": {
            "secret_names": {"remote_write_password": f"deployment:{dep_id}:prom_rw_password"}
        },
        "grafana": {"secret_names": {"api_token": f"deployment:{dep_id}:grafana_token"}},
        "s3": {
            "secret_names": {
                "access_key": f"deployment:{dep_id}:s3_access_key",
                "secret_key": f"deployment:{dep_id}:s3_secret_key",
            }
        },
    }
    store = app.state.secret_store
    created: list[str] = []
    with app.state.session_factory() as db:
        for key, spec in names.items():
            mqtt = key == "mqtt"
            upsert_service(
                db,
                dep_id,
                key,
                config={"note": key},
                secret_names=spec.get("secret_names", {}),
                host="broker.example" if mqtt else None,
                port=8883 if mqtt else None,
                username="platform" if mqtt else None,
                password_secret_name=spec.get("password_secret_name"),
            )
            for name in (
                *spec.get("secret_names", {}).values(),
                *([spec["password_secret_name"]] if mqtt else []),
            ):
                store.put(name, f"v-{key}")
                created.append(name)
        db.commit()
    return created


def test_deleting_a_deployment_removes_its_services_and_still_refuses_on_children(
    delete_app, factory, org_id
):
    """The bug this task fixes: before E5.1 the endpoint passed only `pods`
    and `role_assignments` to the refusal check, so the `mqtt` row every
    deployment carries hit the foreign key and the catch-all returned 500.
    Deletion, not a new refusal - refusing on a row `devbroker` writes for
    every deployment would make deletion permanently impossible."""
    owner = _owner(delete_app)
    created = owner.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": str(org_id), "name": "Service Rich"},
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    dep_id = uuid.UUID(created.json()["id"])
    secret_names = _seed_five_services(delete_app, dep_id)
    assert len(secret_names) == 6

    # The E1 child blockers still refuse, with the service rows present.
    pod_id = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": str(dep_id), "name": "blocker-pod"},
        headers=_csrf(owner),
    ).json()["id"]
    blocked = owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["children"] == {"pods": 1}
    assert owner.delete(f"{API_PREFIX}/pods/{pod_id}", headers=_csrf(owner)).status_code == 204

    with factory() as db:
        user = User(email="e51-scoped@example.com", password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role="field_tech", deployment_id=dep_id))
        db.add(user)
        db.commit()
    blocked_again = owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner))
    assert blocked_again.status_code == 409
    assert blocked_again.json()["error"]["detail"]["children"] == {"role_assignments": 1}
    with factory() as db:
        db.execute(sql_delete(RoleAssignment).where(RoleAssignment.deployment_id == dep_id))
        db.commit()

    # D51: every SecretStore deletion happens strictly AFTER the commit, so a
    # rolled-back transaction can never leave a row pointing at a deleted
    # secret. Observed by checking, from a SEPARATE session, that the
    # deployment row is already gone by the time each delete runs.
    store = delete_app.state.secret_store
    real_delete = store.delete
    committed_first: list[bool] = []

    def observing_delete(name: str) -> None:
        with factory() as probe:
            committed_first.append(probe.get(Deployment, dep_id) is None)
        real_delete(name)

    store.delete = observing_delete  # type: ignore[method-assign]
    try:
        removed = owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner))
    finally:
        store.delete = real_delete  # type: ignore[method-assign]

    assert removed.status_code == 204, removed.text
    assert len(committed_first) == len(secret_names)
    assert all(committed_first), "a secret was deleted before the transaction committed"

    with factory() as db:
        assert (
            db.scalars(
                select(DeploymentService).where(DeploymentService.deployment_id == dep_id)
            ).all()
            == []
        )
        leftover = db.scalars(
            select(Secret.name).where(Secret.name.like(f"deployment:{dep_id}:%"))
        ).all()
        assert leftover == [], f"orphaned deployment secrets: {leftover}"
