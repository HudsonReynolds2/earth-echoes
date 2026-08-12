"""E5.10: generate and download the stack bundle.

The phase document's acceptance, minus the keystone (which lives in
`test_stack_keystone.py` because it needs containers and this file does not):

* **two consecutive downloads are byte-identical** — the determinism that lets
  the platform keep no blob and re-render instead;
* download is `MANAGE_SERVICES`-gated and audited **with no credential in the
  detail**;
* the archive is streamed and never persisted server-side;
* every test that renders a bundle writes to `tmp_path`, so `SECRET_PATTERNS`
  can never match the tree.
"""

import io
import tarfile
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import AuditLog, Deployment, Organization, RoleAssignment, Secret, User
from app.services.store import load_services
from app.settings import Settings

PASSWORD = "stack-endpoint-test-pw"
ROLE_EMAILS = {
    Role.OWNER: "e510-owner@example.com",
    Role.DEPLOYMENT_OPERATOR: "e510-operator@example.com",
    Role.FIELD_TECH: "e510-tech@example.com",
    Role.VIEWER: "e510-viewer@example.com",
}


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
            session_secret="e510-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with factory() as db:
        org = Organization(name="e510-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E510", slug="e510")
        db.add(dep)
        db.flush()
        for role, email in ROLE_EMAILS.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            scope = None if role in (Role.OWNER, Role.VIEWER) else dep.id
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
            db.add(user)
        db.commit()
        application.state.e510_deployment_id = dep.id
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e510_deployment_id


def client_for(app, role: Role) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": ROLE_EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def stack_url(dep_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/deployments/{dep_id}/services/stack"


@pytest.fixture
def owner(app) -> TestClient:
    return client_for(app, Role.OWNER)


@pytest.fixture(autouse=True)
def clean(app, dep_id):
    """Each test starts with no stack, no secrets and no audit rows of its
    own, so the audit assertions count only their own call."""

    def reset():
        store = app.state.secret_store
        with app.state.session_factory() as db:
            for row in load_services(db, dep_id):
                db.delete(row)
            db.commit()
            for name in db.scalars(
                select(Secret.name).where(Secret.name.like(f"deployment:{dep_id}:%"))
            ).all():
                store.delete(name)
            db.execute(sql_delete(AuditLog).where(AuditLog.action.like("services.stack.%")))
            db.commit()

    reset()
    yield
    reset()


def generate(owner: TestClient, dep_id: uuid.UUID, **body):
    return owner.post(stack_url(dep_id), json=body, headers=csrf(owner))


# --- Generation --------------------------------------------------------------


def test_generating_writes_every_service_untested(owner, dep_id):
    """Spec 16.5 gates bundle generation on verification, so a freshly
    generated stack must not arrive already claiming to be verified."""
    response = generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["services"]) == ["grafana", "influx", "mqtt", "prometheus", "s3"]
    # Every row is `untested`, so the spec 16.5 rollup is
    # `pending_verification`: something is configured, none of it has failed,
    # and nothing has been proved. Not `verified` — that is the whole point.
    assert body["services_status"] == "pending_verification"


def test_generation_response_carries_no_credential(owner, dep_id):
    """The POST mints every credential the deployment has. None of them may
    come back in the body — the operator gets them by downloading the archive
    over an authenticated request, not in a JSON response that may be logged."""
    response = generate(owner, dep_id, hostname="broker.example")
    blob = response.text
    for marker in ("BEGIN ", "PRIVATE KEY", "$7$", "$2b$", "$2y$"):
        assert marker not in blob, f"the generate response leaked {marker!r}"


# --- Determinism: the property that lets the platform store nothing ---------


def test_two_consecutive_downloads_are_byte_identical(owner, dep_id):
    """**The acceptance.** The platform keeps no archive and re-renders on
    every request, which is only legitimate if the re-render is the same
    bundle. Two different bundles whose credentials both claim to be current
    is the failure this forbids."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    first = owner.get(f"{stack_url(dep_id)}/download")
    second = owner.get(f"{stack_url(dep_id)}/download")

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert first.content == second.content, (
        "two downloads differed; the platform stores no blob, so a download that is not "
        "reproducible means the operator cannot tell which bundle is current"
    )


def test_the_archive_unpacks_to_the_expected_layout(owner, dep_id, tmp_path):
    """Writes to `tmp_path` on purpose: a bundle rendered anywhere inside the
    repo would put real-shaped credentials where `SECRET_PATTERNS` scans."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    archive = owner.get(f"{stack_url(dep_id)}/download").content

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = sorted(tar.getnames())
    expected = {
        "echoes-stack/docker-compose.yml",
        "echoes-stack/README.md",
        "echoes-stack/.env",
        "echoes-stack/mosquitto/mosquitto.conf",
        "echoes-stack/mosquitto/dynamic-security.json",
        "echoes-stack/mosquitto/ca.crt",
        "echoes-stack/mosquitto/server.crt",
        "echoes-stack/mosquitto/server.key",
        "echoes-stack/prometheus/prometheus.yml",
        "echoes-stack/prometheus/web_config.yml",
    }
    assert expected <= set(names), f"missing: {sorted(expected - set(names))}"


def test_every_archive_entry_is_under_one_directory(owner, dep_id):
    """Unpacking must not scatter files into the operator's cwd."""
    generate(owner, dep_id, hostname="broker.example")
    archive = owner.get(f"{stack_url(dep_id)}/download").content
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for name in tar.getnames():
            assert name.startswith("echoes-stack/"), name


def test_archive_entries_carry_no_host_identity(owner, dep_id):
    """Pinned uid/gid/mtime are what make the bundle reproducible, and they
    also keep the API container's user out of every operator's archive."""
    generate(owner, dep_id, hostname="broker.example")
    archive = owner.get(f"{stack_url(dep_id)}/download").content
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for info in tar.getmembers():
            assert info.mtime == 0
            assert info.uid == 0 and info.gid == 0
            assert info.uname == "" and info.gname == ""


def test_the_private_key_and_env_are_not_world_readable(owner, dep_id):
    """An unpacked bundle on a shared host should at least not hand its
    credentials to every account on the machine."""
    generate(owner, dep_id, hostname="broker.example")
    archive = owner.get(f"{stack_url(dep_id)}/download").content
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        modes = {info.name: info.mode for info in tar.getmembers()}
    assert modes["echoes-stack/.env"] == 0o600
    assert modes["echoes-stack/mosquitto/server.key"] == 0o600


# --- Permissions -------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.FIELD_TECH, Role.VIEWER])
def test_generate_and_download_refuse_roles_without_manage_services(app, dep_id, owner, role):
    """Fixed choice 9: `MANAGE_SERVICES` is Owner and Deployment Operator only.
    A Field Tech provisions hardware and does not hold the deployment's keys to
    everything — and the archive is exactly those keys."""
    generate(owner, dep_id, hostname="broker.example")
    client = client_for(app, role)
    assert client.post(stack_url(dep_id), json={}, headers=csrf(client)).status_code == 403
    assert client.get(f"{stack_url(dep_id)}/download").status_code == 403


def test_the_deployment_operator_may_generate_and_download(app, dep_id):
    operator = client_for(app, Role.DEPLOYMENT_OPERATOR)
    assert (
        operator.post(
            stack_url(dep_id), json={"hostname": "broker.example"}, headers=csrf(operator)
        ).status_code
        == 200
    )
    assert operator.get(f"{stack_url(dep_id)}/download").status_code == 200


def test_downloading_before_generating_is_a_404_not_an_empty_bundle(owner, dep_id):
    """An operator who never generated a stack must get a refusal rather than
    a bundle of blanks that fails mysteriously at `docker compose up`."""
    response = owner.get(f"{stack_url(dep_id)}/download")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --- Audit -------------------------------------------------------------------


def test_the_download_is_audited_with_no_credential_in_the_detail(app, owner, dep_id):
    """**The acceptance.** Who took the deployment's credentials and when is
    exactly what the record is for; what was in them is not."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    archive = owner.get(f"{stack_url(dep_id)}/download").content

    with app.state.session_factory() as db:
        row = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "services.stack.download")
            .order_by(AuditLog.at.desc())
        )
    assert row is not None, "the download wrote no audit row"
    assert row.detail == {"bytes": len(archive)}, (
        "the download audit detail is the size and nothing else; anything richer risks "
        "becoming a second copy of what the archive contained"
    )


def test_the_generation_is_audited_with_choices_not_credentials(app, owner, dep_id):
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    with app.state.session_factory() as db:
        row = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "services.stack.generate")
            .order_by(AuditLog.at.desc())
        )
    assert row is not None
    assert row.detail["include_object_storage"] is True
    assert sorted(row.detail["services"]) == ["grafana", "influx", "mqtt", "prometheus", "s3"]
    for marker in ("BEGIN ", "PRIVATE KEY", "$7$", "$2y$"):
        assert marker not in str(row.detail)


# --- Nothing is persisted server-side ----------------------------------------


def test_no_bundle_is_written_to_disk_by_a_download(owner, dep_id, tmp_path, monkeypatch):
    """Fixed choice 7: no blob column, no temp directory, no cleanup job. The
    archive is built in memory, so there is no path on this host that ever
    holds a deployment's credentials in the clear."""
    import tempfile

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp
    real_named = tempfile.NamedTemporaryFile

    def spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    def spy_named(*args, **kwargs):
        handle = real_named(*args, **kwargs)
        created.append(handle.name)
        return handle

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", spy_named)

    generate(owner, dep_id, hostname="broker.example")
    response = owner.get(f"{stack_url(dep_id)}/download")
    assert response.status_code == 200
    assert created == [], f"the download wrote to disk: {created}"
