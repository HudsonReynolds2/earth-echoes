"""Gate 22: E1.3 one aggregator per pod (spec 13, 4.2).

Both creation paths from the acceptance criteria - inline create-and-attach
in one call, and bare-pod-then-attach - plus the occupied-pod 409 and the
transaction guarantee: a failed inline aggregator rolls the pod back too.
"""

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import AuditLog, Organization, Pod, RoleAssignment, User
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "podagg-owner@example.com"


@pytest.fixture(scope="module")
def pa_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate22-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="podagg-org")
        db.add(org)
        db.flush()
        app.state.org_id = str(org.id)
        user = User(email=OWNER, password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        db.add(user)
        db.commit()
    return app


@pytest.fixture(scope="module")
def owner(pa_app):
    client = TestClient(pa_app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


@pytest.fixture(scope="module")
def dep_id(pa_app, owner):
    response = owner.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": pa_app.state.org_id, "name": "Pod-Agg Deployment"},
        headers=_csrf(owner),
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_create_pod_with_inline_aggregator_one_call_two_audit_rows(pa_app, owner, dep_id):
    created = owner.post(
        f"{API_PREFIX}/pods",
        json={
            "deployment_id": dep_id,
            "name": "Inline Pod",
            "aggregator": {"aggregator_uuid": "podagg-inline-01", "name": "agg-inline"},
        },
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["aggregator"]["aggregator_uuid"] == "podagg-inline-01"

    factory = pa_app.state.session_factory
    with factory() as db:
        actions = db.scalars(
            select(AuditLog.action)
            .where(AuditLog.action.in_(["pod.create", "aggregator.create"]))
            .where(AuditLog.detail["name"].astext == "Inline Pod")
        ).all()
        agg_rows = db.scalars(
            select(AuditLog.action).where(
                AuditLog.detail["aggregator_uuid"].astext == "podagg-inline-01"
            )
        ).all()
    assert "pod.create" in actions
    assert agg_rows == ["aggregator.create"]


def test_inline_aggregator_generates_platform_uuid_when_omitted(owner, dep_id):
    created = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": dep_id, "name": "Inline Pod 2", "aggregator": {}},
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    assert len(created.json()["aggregator"]["aggregator_uuid"]) == 32  # uuid4().hex


def test_bare_pod_then_attach_then_occupied_409(owner, dep_id):
    pod = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": dep_id, "name": "Bare Pod"},
        headers=_csrf(owner),
    )
    assert pod.status_code == 201
    assert pod.json()["aggregator"] is None
    pod_id = pod.json()["id"]

    attached = owner.post(
        f"{API_PREFIX}/aggregators",
        json={"pod_id": pod_id, "aggregator_uuid": "podagg-attach-01"},
        headers=_csrf(owner),
    )
    assert attached.status_code == 201

    squatter = owner.post(
        f"{API_PREFIX}/aggregators",
        json={"pod_id": pod_id, "aggregator_uuid": "podagg-attach-02"},
        headers=_csrf(owner),
    )
    assert squatter.status_code == 409
    assert squatter.json()["error"]["code"] == "conflict"


def test_failed_inline_aggregator_rolls_the_pod_back(pa_app, owner, dep_id):
    """The one-call semantics are transactional: a duplicate aggregator_uuid
    must not leave a bare pod behind."""
    doomed = owner.post(
        f"{API_PREFIX}/pods",
        json={
            "deployment_id": dep_id,
            "name": "Doomed Pod",
            "aggregator": {"aggregator_uuid": "podagg-inline-01"},  # taken above
        },
        headers=_csrf(owner),
    )
    assert doomed.status_code == 409
    factory = pa_app.state.session_factory
    with factory() as db:
        count = db.scalar(select(func.count()).where(Pod.name == "Doomed Pod"))
    assert count == 0
    # And its pod.create audit row rolled back with it - one commit seals all.
    with factory() as db:
        audit = db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.detail["name"].astext == "Doomed Pod")
        )
    assert audit == 0
