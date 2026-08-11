"""E5.2: the write-only deployment services API (spec 16.2, 13; D51).

The endpoint's whole reason to exist is that it accepts credentials and never
returns them, so the suite is built around proving absence rather than
presence:

1. **No submitted plaintext appears anywhere in a response**, asserted by
   scanning the SERIALIZED body for each literal value. Checking field names
   would pass just as happily against a body that leaked a token under a name
   the test forgot to list.
2. **The round trip holds.** `PUT(body) -> GET -> PUT(that body) -> GET`
   leaves identical secret NAMES on the rows and identical `SecretStore.get`
   plaintexts behind them, which is the property that lets the S5 wizard save
   a form it never held the secrets for.
3. **All four roles**, against both verbs.
4. **The audit row names fields and never values**, asserted the same way as
   (1) - by scanning the serialized detail.
"""

import json
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.devbroker import secret_name as broker_secret_name
from app.main import API_PREFIX, create_app
from app.models import SERVICE_KEYS, AuditLog, Deployment, Organization, RoleAssignment, User
from app.secrets import SecretStoreError
from app.services.schemas import SERVICE_SCHEMAS, secret_name
from app.services.store import load_service, load_services
from app.settings import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"  # noqa: S105  (test account, not a credential)

#: A stand-in trust anchor. A CERTIFICATE header and never a private key one,
#: so `test_repo_layout.SECRET_PATTERNS` stays quiet.
FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

#: Every plaintext the suite submits. Deliberately long, distinctive and
#: unlikely to occur by accident, because the central assertion is a SUBSTRING
#: search over a serialized response: a plaintext of "x" would pass trivially.
PLAINTEXTS = {
    "mqtt.password": "PLAINTEXT-mqtt-password-6c1f9a",
    "influx.token": "PLAINTEXT-influx-token-4b7e02",
    "prometheus.remote_write_password": "PLAINTEXT-prom-rw-password-9d3a55",
    "grafana.service_account_token": "PLAINTEXT-grafana-token-1e8fb4",
    "s3.access_key": "PLAINTEXT-s3-access-key-77ca10",
    "s3.secret_key": "PLAINTEXT-s3-secret-key-0af23d",
}

ROLE_EMAILS = {
    Role.OWNER: "e52-owner@example.com",
    Role.DEPLOYMENT_OPERATOR: "e52-operator@example.com",
    Role.FIELD_TECH: "e52-tech@example.com",
    Role.VIEWER: "e52-viewer@example.com",
}


def full_body() -> dict[str, object]:
    """All five services with every secret set. One function, so a field added
    to a schema without a test using it is visible in one place."""
    return {
        "services": {
            "mqtt": {
                "host": "broker.example",
                "port": 8883,
                "tls_enabled": True,
                "ca_cert_pem": FAKE_CA_PEM,
                "username": "platform",
                "password": PLAINTEXTS["mqtt.password"],
            },
            "influx": {
                "url": "https://influx.example:8181",
                "database": "recordings",
                "token": PLAINTEXTS["influx.token"],
            },
            "prometheus": {
                "read_url": "https://prom.example:9090",
                "remote_write_url": "https://prom.example:9090/api/v1/write",
                "remote_write_user": "aggregator",
                "remote_write_password": PLAINTEXTS["prometheus.remote_write_password"],
            },
            "grafana": {
                "base_url": "https://grafana.example:3000",
                "service_account_token": PLAINTEXTS["grafana.service_account_token"],
            },
            "s3": {
                "bucket": "eoe-audio",
                "region": "us-east-1",
                "endpoint": "https://minio.example:9000",
                "access_key": PLAINTEXTS["s3.access_key"],
                "secret_key": PLAINTEXTS["s3.secret_key"],
            },
        }
    }


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def factory(pg_url):
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def app(pg_url, factory):
    application = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e52-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with factory() as db:
        org = Organization(name="e52-org")
        db.add(org)
        db.flush()
        # The scoped roles are scoped to `dep`, so an operator's grant on this
        # deployment is what the endpoint checks against.
        dep = Deployment(organization_id=org.id, name="E52", slug="e52")
        other = Deployment(organization_id=org.id, name="E52 other", slug="e52-other")
        db.add_all([dep, other])
        db.flush()
        for role, email in ROLE_EMAILS.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            scope = None if role in (Role.OWNER, Role.VIEWER) else dep.id
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
            db.add(user)
        db.commit()
        application.state.e52_deployment_id = dep.id
        application.state.e52_other_deployment_id = other.id
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e52_deployment_id


def client_for(app, role: Role) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": ROLE_EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def url(dep_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/deployments/{dep_id}/services"


@pytest.fixture
def owner(app) -> TestClient:
    return client_for(app, Role.OWNER)


def reset_services(app, dep_id: uuid.UUID) -> None:
    """Between tests: drop this deployment's rows, their secrets and the audit
    rows they wrote, so each test starts from `configured: false` on all five
    and the two audit assertions count only their OWN save."""
    store = app.state.secret_store
    with app.state.session_factory() as db:
        for row in load_services(db, dep_id):
            names = [*row.secret_names.values()]
            if row.password_secret_name:
                names.append(row.password_secret_name)
            db.delete(row)
            db.commit()
            for name in names:
                store.delete(name)
        db.execute(
            sql_delete(AuditLog).where(
                AuditLog.action == "services.update", AuditLog.entity_id == str(dep_id)
            )
        )
        db.commit()


@pytest.fixture(autouse=True)
def clean(app, dep_id):
    reset_services(app, dep_id)
    yield
    reset_services(app, dep_id)


# --- 1. no plaintext ever comes back -----------------------------------------


def assert_no_plaintext(payload: object, where: str) -> None:
    """The central assertion. Serialize, then search for each literal value -
    a field-name check would pass against a body leaking a token under a name
    this test did not think to list."""
    blob = json.dumps(payload, default=str)
    for field, value in PLAINTEXTS.items():
        assert value not in blob, f"{where} leaked the plaintext for {field}"


def test_a_full_save_never_echoes_a_single_submitted_secret(owner, dep_id):
    put = owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    assert put.status_code == 200, put.text
    assert_no_plaintext(put.json(), "the PUT response")

    got = owner.get(url(dep_id))
    assert got.status_code == 200
    assert_no_plaintext(got.json(), "the GET response")

    # And the redaction is the keep sentinel, not an omission that would make
    # the wizard show an empty field for a credential that IS set.
    services = got.json()["services"]
    assert services["influx"]["settings"]["token"] == {"$secret_set": True}
    assert services["s3"]["settings"]["access_key"] == {"$secret_set": True}
    assert services["mqtt"]["settings"]["password"] == {"$secret_set": True}


def test_the_get_returns_all_five_in_spec_order_configured_or_not(owner, dep_id):
    body = owner.get(url(dep_id)).json()
    assert list(body["services"]) == list(SERVICE_KEYS)
    assert all(service["configured"] is False for service in body["services"].values())
    assert all(service["status"] == "untested" for service in body["services"].values())

    owner.put(
        url(dep_id),
        json={"services": {"influx": full_body()["services"]["influx"]}},
        headers=csrf(owner),
    )
    body = owner.get(url(dep_id)).json()
    assert list(body["services"]) == list(SERVICE_KEYS)
    assert body["services"]["influx"]["configured"] is True
    assert body["services"]["grafana"]["configured"] is False


# --- 2. the round trip -------------------------------------------------------


def stored_state(app, dep_id: uuid.UUID) -> dict[str, object]:
    """Secret NAMES on the rows, and the plaintext behind each - the two
    things the round trip must not disturb."""
    store = app.state.secret_store
    state: dict[str, object] = {}
    with app.state.session_factory() as db:
        for row in load_services(db, dep_id):
            names = dict(row.secret_names)
            if row.password_secret_name:
                names["password"] = row.password_secret_name
            state[row.service_key] = {
                "config": dict(row.config),
                "names": names,
                "values": {field: store.get(name) for field, name in names.items()},
            }
    return state


def test_a_form_built_from_a_get_round_trips_without_holding_the_secrets(app, owner, dep_id):
    """The property the S5 wizard depends on: an operator opens the services
    page, edits one endpoint and saves, and the four credentials they never
    saw survive untouched."""
    owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    before = stored_state(app, dep_id)

    first_get = owner.get(url(dep_id)).json()
    # Exactly what a client can build from the response - keep sentinels in
    # every secret field, no plaintext anywhere.
    resubmit = {
        "services": {
            key: service["settings"]
            for key, service in first_get["services"].items()
            if service["configured"]
        }
    }
    assert_no_plaintext(resubmit, "the round-trip body")

    second = owner.put(url(dep_id), json=resubmit, headers=csrf(owner))
    assert second.status_code == 200, second.text
    assert stored_state(app, dep_id) == before
    assert owner.get(url(dep_id)).json() == first_get


def test_secret_names_are_deterministic_and_the_broker_name_matches_devbroker(app, owner, dep_id):
    """`devbroker.secret_name` and `schemas.secret_name` must mint the same
    name for the broker password, or Path A rewriting broker credentials
    through this API would leave `load_broker_coordinates` reading a stale
    secret under the old name."""
    owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    with app.state.session_factory() as db:
        mqtt = load_service(db, dep_id, "mqtt")
        assert mqtt.password_secret_name == broker_secret_name(dep_id)
        assert mqtt.password_secret_name == secret_name(dep_id, "mqtt", "password")
        assert mqtt.secret_names == {}, "the broker password lives in its column, not the map"

        s3 = load_service(db, dep_id, "s3")
        assert s3.secret_names == {
            "access_key": secret_name(dep_id, "s3", "access_key"),
            "secret_key": secret_name(dep_id, "s3", "secret_key"),
        }
    assert app.state.secret_store.get(broker_secret_name(dep_id)) == PLAINTEXTS["mqtt.password"]


def test_a_service_absent_from_the_body_is_untouched(app, owner, dep_id):
    owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    before = stored_state(app, dep_id)

    only_grafana = {
        "services": {
            "grafana": {
                "base_url": "https://grafana.moved:3000",
                "service_account_token": {"$secret_set": True},
            }
        }
    }
    assert owner.put(url(dep_id), json=only_grafana, headers=csrf(owner)).status_code == 200

    after = stored_state(app, dep_id)
    assert after["mqtt"] == before["mqtt"]
    assert after["influx"] == before["influx"]
    assert after["s3"] == before["s3"]
    assert after["grafana"]["config"]["base_url"] == "https://grafana.moved:3000"
    assert after["grafana"]["values"] == before["grafana"]["values"]


def test_a_present_service_is_replaced_wholesale_not_merged(app, owner, dep_id):
    """PUT is never a merge (the E1.7 tags precedent). Dropping `region`
    really drops it, rather than leaving a stale value the UI cannot see."""
    owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    trimmed = {
        "services": {
            "s3": {
                "bucket": "eoe-audio",
                "access_key": {"$secret_set": True},
                "secret_key": {"$secret_set": True},
            }
        }
    }
    assert owner.put(url(dep_id), json=trimmed, headers=csrf(owner)).status_code == 200
    settings = owner.get(url(dep_id)).json()["services"]["s3"]["settings"]
    assert "region" not in settings
    assert "endpoint" not in settings
    assert settings["bucket"] == "eoe-audio"


def test_omitting_a_secret_unsets_it_and_deletes_the_stored_value(app, owner, dep_id):
    owner.put(url(dep_id), json=full_body(), headers=csrf(owner))
    orphaned = secret_name(dep_id, "s3", "secret_key")
    assert app.state.secret_store.exists(orphaned)

    dropped = {
        "services": {
            "s3": {"bucket": "eoe-audio", "access_key": {"$secret_set": True}},
        }
    }
    assert owner.put(url(dep_id), json=dropped, headers=csrf(owner)).status_code == 200

    assert not app.state.secret_store.exists(orphaned)
    with app.state.session_factory() as db:
        assert "secret_key" not in load_service(db, dep_id, "s3").secret_names
    settings = owner.get(url(dep_id)).json()["services"]["s3"]["settings"]
    assert "secret_key" not in settings
    assert settings["access_key"] == {"$secret_set": True}


# --- 3. the boundary rejects what it should ----------------------------------


def test_a_keep_sentinel_for_a_credential_that_is_not_set_is_a_422(owner, dep_id):
    """A real client bug - a form built from a GET that said the field was
    empty. Writing nothing would leave the operator believing they saved."""
    body = {
        "services": {
            "grafana": {
                "base_url": "https://grafana.example:3000",
                "service_account_token": {"$secret_set": True},
            }
        }
    }
    response = owner.put(url(dep_id), json=body, headers=csrf(owner))
    assert response.status_code == 422, response.text
    detail = response.json()["error"]["detail"]["errors"][0]
    assert (detail["service_key"], detail["field"], detail["code"]) == (
        "grafana",
        "service_account_token",
        "no_stored_secret",
    )


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"services": {"loki": {"url": "https://loki.example"}}}, "a sixth service key"),
        (
            {"services": {"influx": {"url": "u", "database": "d", "tokenn": "typo"}}},
            "a misspelled field",
        ),
        (
            {"services": {"mqtt": {"host": "h", "port": 8883, "username": "u"}}},
            "an mqtt row with no password (the CHECK would 500)",
        ),
        (
            {"services": {"mqtt": {"host": "h", "port": 70000, "username": "u", "password": "p"}}},
            "a port outside the port_range CHECK",
        ),
        (
            {
                "services": {
                    "influx": {"url": "u", "database": "d", "token": {"$secret_set": False}}
                }
            },
            "a near-miss sentinel",
        ),
    ],
)
def test_the_write_boundary_rejects_it(owner, dep_id, body, why):
    """Every one of these would otherwise reach the database and become an
    IntegrityError the catch-all turns into a 500, or a key that silently
    never reaches a device."""
    response = owner.put(url(dep_id), json=body, headers=csrf(owner))
    assert response.status_code == 422, f"{why} was accepted: {response.text}"


def test_every_service_key_has_a_schema():
    assert tuple(SERVICE_SCHEMAS) == SERVICE_KEYS


# --- 4. permissions, CSRF and the audit trail --------------------------------


@pytest.mark.parametrize(
    ("role", "put_status"),
    [
        (Role.OWNER, 200),
        (Role.DEPLOYMENT_OPERATOR, 200),
        (Role.FIELD_TECH, 403),
        (Role.VIEWER, 403),
    ],
)
def test_all_four_roles_read_and_only_two_write(app, dep_id, role, put_status):
    client = client_for(app, role)
    assert client.get(url(dep_id)).status_code == 200
    response = client.put(url(dep_id), json=full_body(), headers=csrf(client))
    assert response.status_code == put_status, response.text


def test_an_operator_scoped_elsewhere_cannot_write(app):
    other = app.state.e52_other_deployment_id
    client = client_for(app, Role.DEPLOYMENT_OPERATOR)
    assert client.put(url(other), json=full_body(), headers=csrf(client)).status_code == 403


def test_the_put_needs_csrf(owner, dep_id):
    assert owner.put(url(dep_id), json=full_body()).status_code == 403


def test_an_unknown_deployment_is_404_for_an_org_wide_owner(owner):
    assert owner.get(url(uuid.uuid4())).status_code == 404


def test_the_audit_row_names_fields_and_never_values(app, owner, dep_id):
    assert owner.put(url(dep_id), json=full_body(), headers=csrf(owner)).status_code == 200
    with app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "services.update", AuditLog.entity_id == str(dep_id)
                )
            )
        )
    assert len(rows) == 1
    detail = rows[0].detail
    assert set(detail["services"]) == set(SERVICE_KEYS)
    assert "password" in detail["services"]["mqtt"]
    assert "token" in detail["services"]["influx"]
    assert_no_plaintext(detail, "the audit detail")
    assert rows[0].scope == dep_id


def test_nothing_is_audited_when_nothing_is_submitted(app, owner, dep_id):
    assert owner.put(url(dep_id), json={"services": {}}, headers=csrf(owner)).status_code == 200
    with app.state.session_factory() as db:
        assert not list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "services.update", AuditLog.entity_id == str(dep_id)
                )
            )
        )


def test_a_rejected_save_writes_no_row_and_no_secret(app, owner, dep_id):
    """The plans are all decided before anything is written, so the valid
    influx block in this body must not survive the invalid grafana one."""
    body = {
        "services": {
            "influx": full_body()["services"]["influx"],
            "grafana": {
                "base_url": "https://grafana.example:3000",
                "service_account_token": {"$secret_set": True},
            },
        }
    }
    assert owner.put(url(dep_id), json=body, headers=csrf(owner)).status_code == 422
    with app.state.session_factory() as db:
        assert load_services(db, dep_id) == []
    with pytest.raises(SecretStoreError):
        app.state.secret_store.get(secret_name(dep_id, "influx", "token"))
