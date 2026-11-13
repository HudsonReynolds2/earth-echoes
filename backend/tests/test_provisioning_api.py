"""E4.8: the export archive and the spec 13 bundle endpoints (phase-4 E4.8;
fixed choice 4; D178).

Acceptance:

1. **A downloaded archive's manifest checksums verify against its own
   members.**
2. **A secret-leak scan over the whole archive and every API response in the
   suite**: the plaintext PSK and stream key appear in neither (rule R2, spec
   14.1). `scanned` records every response this module receives. Its teardown
   fails the suite if any one carried a planted value, and download archives
   are DECOMPRESSED before the scan, because deflate would hide plaintext from
   a byte search.
3. **Regenerating a bundle for the same Pod makes a new bundle row** and never
   mutates the old one, whose archive still downloads byte for byte.

Plus RBAC (MANAGE to generate and download, VIEW to read, out-of-scope = the
same 404), the 422s with what to fix, audit rows, and the Aggregator's
plaintext login appearing only where spec 16.4 puts it.
"""

import json
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.config.overrides import put_overrides
from app.db import create_session_factory
from app.devbroker import plan_accounts, register_services
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    Deployment,
    DeviceProvisioningRecord,
    Listener,
    Pod,
    ProvisioningBundle,
    RoleAssignment,
    User,
)
from app.provisioning.archive import read_zip
from app.provisioning.manifest import MANIFEST_PATH, sha256, verify_members
from app.seed import seed_demo_hierarchy
from app.services.credentials import DeviceCredential
from app.settings import Settings

pytestmark = pytest.mark.integration

PSK = "e48-planted-psk-2b7e91-never-in-a-response"
STREAM = "e48-planted-stream-d40c55-never-in-a-response"
DEVICE_LOGIN_MARKER = "e48-planted-device-login-91fa30"
LOGIN_PASSWORD = "correct horse battery staple"  # noqa: S105  (test account)
CA = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

NETWORK = {
    "network.wifi_ssid": "e48-halow",
    "network.wifi_password": PSK,
    "network.stream_key": STREAM,
    "network.aggregator_ip": "10.48.0.1",
    "network.stream_endpoint": "10.48.0.1:8765/stream",
}

ROLE_EMAILS = {
    Role.OWNER: "e48-owner@example.com",
    Role.FIELD_TECH: "e48-tech@example.com",
    Role.VIEWER: "e48-viewer@example.com",
    Role.DEPLOYMENT_OPERATOR: "e48-other-op@example.com",
}


class FakeProvider:
    def __init__(self) -> None:
        self.minted: list[str] = []

    async def mint(self, coordinates, aggregator_uuid: str) -> DeviceCredential:
        self.minted.append(aggregator_uuid)
        return DeviceCredential(username=f"agg-{aggregator_uuid}", password=DEVICE_LOGIN_MARKER)

    async def revoke(self, coordinates, aggregator_uuid: str) -> None:  # pragma: no cover
        raise AssertionError("never revoked here")


SCANNED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def scanned():
    """Acceptance 2's collector. Every response body this module receives is
    appended (archives as their decompressed members), then scanned once at
    the end so no test can forget to."""
    SCANNED.clear()
    yield SCANNED
    assert SCANNED, "the leak scan saw no responses: the collector is not wired"
    for index, body in enumerate(SCANNED):
        for secret in (PSK, STREAM):
            assert secret not in body, f"response #{index} leaked a planted secret"


def _seen(response):
    if response.headers.get("content-type", "").startswith("application/zip"):
        for path, content in read_zip(response.content).items():
            SCANNED.append(f"{path}\n" + content.decode("utf-8", "replace"))
        SCANNED.append(response.content.decode("latin-1"))
    else:
        SCANNED.append(response.text)
    return response


class Client:
    """A logged-in TestClient whose every response goes through `_seen`."""

    def __init__(self, app, role: Role) -> None:
        self.http = TestClient(app, raise_server_exceptions=False)
        response = self.http.post(
            f"{API_PREFIX}/auth/login",
            json={"email": ROLE_EMAILS[role], "password": LOGIN_PASSWORD},
        )
        assert response.status_code == 200, response.text

    def get(self, path: str, **kwargs):
        return _seen(self.http.get(f"{API_PREFIX}{path}", **kwargs))

    def post(self, path: str, body: dict):
        headers = {"X-CSRF-Token": self.http.cookies["eoe_csrf"]}
        return _seen(self.http.post(f"{API_PREFIX}{path}", json=body, headers=headers))


@pytest.fixture(scope="module")
def app():
    with ephemeral_postgres() as url:
        _, factory = create_session_factory(url)
        application = create_app(
            Settings(
                database_url=url,
                session_secret="e48-secret",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
            )
        )
        application.state.credential_provider = FakeProvider()
        store = application.state.secret_store
        with factory() as db:
            seed_demo_hierarchy(db)
            deployments = list(db.scalars(select(Deployment).order_by(Deployment.slug)))
            mine, other = deployments[0], deployments[1]
            pods = list(
                db.scalars(
                    select(Pod)
                    .join(Aggregator, Aggregator.pod_id == Pod.id)
                    .where(Pod.deployment_id == mine.id)
                    .order_by(Pod.name)
                )
            )
            # Every Pod of `mine` gets network values except the LAST, which
            # stays unconfigured for the 422 test.
            for pod in pods[:-1]:
                put_overrides(db, store, "pod", str(pod.id), NETWORK)
            db.commit()
            register_services(
                db, store, plan_accounts(db), host="broker.example", port=8883, ca_cert_pem=CA
            )
            db.execute(update(Deployment).values(services_status="verified"))
            for role, email in ROLE_EMAILS.items():
                user = User(email=email, password_hash=hash_password(LOGIN_PASSWORD))
                scope = {
                    Role.OWNER: None,
                    Role.VIEWER: None,
                    Role.FIELD_TECH: mine.id,
                    Role.DEPLOYMENT_OPERATOR: other.id,
                }[role]
                user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
                db.add(user)
            db.commit()
            application.state.e48 = {
                "deployment": mine.id,
                "deployment_name": mine.name,
                "other": other.id,
                "pods": [p.id for p in pods],
            }
        yield application


@pytest.fixture(scope="module")
def owner(app):
    return Client(app, Role.OWNER)


def _pod_body(app, index: int = 0, **extra) -> dict:
    e48 = app.state.e48
    return {
        "deployment_id": str(e48["deployment"]),
        "scope": "pod",
        "pods": [{"pod_id": str(e48["pods"][index]), **extra}],
    }


def _listener_count(app, pod_id) -> int:
    with app.state.session_factory() as db:
        aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == pod_id))
        return len(
            db.scalars(select(Listener.mac).where(Listener.aggregator_id == aggregator.id)).all()
        )


# --- Generation ------------------------------------------------------------------


def test_generating_a_pod_bundle_records_one_card_per_device(app, owner):
    response = owner.post("/provisioning/bundles", _pod_body(app))
    assert response.status_code == 201, response.text
    bundle = response.json()

    pod_id = app.state.e48["pods"][0]
    assert bundle["scope_type"] == "pod"
    assert bundle["scope_id"] == str(pod_id)
    assert bundle["record_count"] == _listener_count(app, pod_id) + 1
    statuses = {record["status"] for record in bundle["records"]}
    assert statuses == {"generated"}
    assert all(r["status_history"][0]["status"] == "generated" for r in bundle["records"])
    kinds = sorted({record["file_kind"] for record in bundle["records"]})
    assert kinds == ["pod_file", "settings_yaml"]

    with app.state.session_factory() as db:
        audit = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "provisioning.bundle.generate",
                AuditLog.entity_id == bundle["id"],
            )
        ).all()
        assert len(audit) == 1


def test_a_deployment_bundle_mixes_modes_and_skips_nothing_it_can_provision(app, owner):
    e48 = app.state.e48
    configured = e48["pods"][:-1]
    # The unconfigured last Pod would be refused, so this bundle covers a
    # deployment whose every Pod is configured: temporarily configure it.
    with app.state.session_factory() as db:
        put_overrides(db, app.state.secret_store, "pod", str(e48["pods"][-1]), NETWORK)
        db.commit()
    try:
        first_macs = []
        with app.state.session_factory() as db:
            aggregator = db.scalar(select(Aggregator).where(Aggregator.pod_id == configured[0]))
            first_macs = list(
                db.scalars(
                    select(Listener.mac).where(Listener.aggregator_id == aggregator.id).limit(1)
                )
            )
        response = owner.post(
            "/provisioning/bundles",
            {
                "deployment_id": str(e48["deployment"]),
                "scope": "deployment",
                "pods": [
                    {"pod_id": str(configured[0]), "mode": "mixed", "listener_macs": first_macs}
                ],
            },
        )
        assert response.status_code == 201, response.text
        bundle = response.json()
        assert set(bundle["plan"]) == {str(p) for p in e48["pods"]}
        assert bundle["plan"][str(configured[0])]["mode"] == "mixed"
        expected = sum(_listener_count(app, p) + 1 for p in e48["pods"])
        assert bundle["record_count"] == expected
    finally:
        with app.state.session_factory() as db:
            put_overrides(db, app.state.secret_store, "pod", str(e48["pods"][-1]), {})
            db.commit()


# --- Download -------------------------------------------------------------------------


def test_the_downloaded_archive_verifies_against_its_own_manifest(app, owner):
    bundle = owner.post("/provisioning/bundles", _pod_body(app)).json()
    response = owner.get(f"/provisioning/bundles/{bundle['id']}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert sha256(response.content) == bundle["archive_checksum"]

    members = read_zip(response.content)
    assert verify_members(members) == []
    manifest = json.loads(members[MANIFEST_PATH])
    for entry in manifest["files"]:
        assert entry["sha256"] == sha256(members[entry["path"]])
    # Spec 16.4: the Aggregator's login is plaintext in ITS file, by design,
    # and nowhere else in the archive.
    holders = [path for path, content in members.items() if DEVICE_LOGIN_MARKER.encode() in content]
    assert holders == [p for p in members if p.endswith("aggregator/settings.yaml")]

    with app.state.session_factory() as db:
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "provisioning.bundle.download",
                AuditLog.entity_id == bundle["id"],
            )
        )
        assert audit.detail == {"bytes": len(response.content)}


def test_regenerating_makes_a_new_bundle_and_the_old_one_is_untouched(app, owner):
    first = owner.post("/provisioning/bundles", _pod_body(app)).json()
    first_archive = owner.get(f"/provisioning/bundles/{first['id']}/download").content
    second = owner.post("/provisioning/bundles", _pod_body(app)).json()

    assert second["id"] != first["id"]
    assert second["archive_checksum"] != first["archive_checksum"], "fresh key material"
    again = owner.get(f"/provisioning/bundles/{first['id']}").json()
    assert again["archive_checksum"] == first["archive_checksum"]
    assert owner.get(f"/provisioning/bundles/{first['id']}/download").content == first_archive
    with app.state.session_factory() as db:
        old_records = db.scalars(
            select(DeviceProvisioningRecord).where(
                DeviceProvisioningRecord.bundle_id == uuid.UUID(first["id"])
            )
        ).all()
        assert len(old_records) == first["record_count"]


# --- Reading -------------------------------------------------------------------------


def test_list_and_get_use_the_d7_envelope_and_scope(app, owner):
    created = owner.post("/provisioning/bundles", _pod_body(app)).json()
    listing = owner.get(
        "/provisioning/bundles", params={"deployment_id": str(app.state.e48["deployment"])}
    ).json()
    assert set(listing) == {"items", "total", "limit", "offset"}
    assert created["id"] in {item["id"] for item in listing["items"]}
    detail = owner.get(f"/provisioning/bundles/{created['id']}").json()
    assert len(detail["records"]) == created["record_count"]


# --- Refusals -----------------------------------------------------------------------


def test_an_unconfigured_pod_is_a_422_naming_the_missing_keys(app, owner):
    response = owner.post("/provisioning/bundles", _pod_body(app, index=-1))
    assert response.status_code == 422
    error = response.json()["error"]
    assert "network.wifi_ssid" in error["detail"]["missing"]
    assert "network.wifi_ssid" in error["message"]


def test_an_unverified_deployment_is_a_422_naming_it(app, owner):
    e48 = app.state.e48
    with app.state.session_factory() as db:
        db.get(Deployment, e48["deployment"]).services_status = "degraded"
        db.commit()
    try:
        response = owner.post("/provisioning/bundles", _pod_body(app))
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["detail"] == {"deployment": e48["deployment_name"]}
        assert e48["deployment_name"] in error["message"]
    finally:
        with app.state.session_factory() as db:
            db.get(Deployment, e48["deployment"]).services_status = "verified"
            db.commit()


@pytest.mark.parametrize(
    ("listener_macs", "status"),
    [(["02:00:00:00:E4:48"], 422), (["not-a-mac"], 422)],
)
def test_a_bad_or_out_of_pod_mac_is_a_422(app, owner, listener_macs, status):
    response = owner.post(
        "/provisioning/bundles", _pod_body(app, mode="mixed", listener_macs=listener_macs)
    )
    assert response.status_code == status


def test_a_pod_from_another_deployment_and_an_unknown_bundle_are_404s(app, owner):
    e48 = app.state.e48
    with app.state.session_factory() as db:
        foreign = db.scalar(select(Pod.id).where(Pod.deployment_id == e48["other"]))
    body = {
        "deployment_id": str(e48["deployment"]),
        "scope": "pod",
        "pods": [{"pod_id": str(foreign)}],
    }
    assert owner.post("/provisioning/bundles", body).status_code == 404
    assert owner.get(f"/provisioning/bundles/{uuid.uuid4()}").status_code == 404


def test_a_refused_bundle_records_and_stores_nothing(app, owner):
    with app.state.session_factory() as db:
        before = db.scalar(select(ProvisioningBundle.id).order_by(ProvisioningBundle.id).limit(1))
        count = len(db.scalars(select(ProvisioningBundle.id)).all())
    assert owner.post("/provisioning/bundles", _pod_body(app, index=-1)).status_code == 422
    with app.state.session_factory() as db:
        assert len(db.scalars(select(ProvisioningBundle.id)).all()) == count
    assert before is None or before


# --- RBAC -------------------------------------------------------------------------------


def test_a_viewer_reads_but_cannot_generate_or_download(app, owner):
    bundle = owner.post("/provisioning/bundles", _pod_body(app)).json()
    viewer = Client(app, Role.VIEWER)
    assert viewer.get(f"/provisioning/bundles/{bundle['id']}").status_code == 200
    assert viewer.get("/provisioning/bundles").status_code == 200
    assert viewer.post("/provisioning/bundles", _pod_body(app)).status_code == 404
    assert viewer.get(f"/provisioning/bundles/{bundle['id']}/download").status_code == 404


def test_a_field_tech_provisions_their_own_deployment(app):
    tech = Client(app, Role.FIELD_TECH)
    response = tech.post("/provisioning/bundles", _pod_body(app))
    assert response.status_code == 201, response.text
    assert tech.get(f"/provisioning/bundles/{response.json()['id']}/download").status_code == 200


def test_another_deployments_operator_sees_nothing(app, owner):
    bundle = owner.post("/provisioning/bundles", _pod_body(app)).json()
    outsider = Client(app, Role.DEPLOYMENT_OPERATOR)
    assert outsider.get(f"/provisioning/bundles/{bundle['id']}").status_code == 404
    assert outsider.get(f"/provisioning/bundles/{bundle['id']}/download").status_code == 404
    assert outsider.post("/provisioning/bundles", _pod_body(app)).status_code == 404
    listing = outsider.get("/provisioning/bundles").json()
    assert bundle["id"] not in {item["id"] for item in listing["items"]}
