"""Gate 26: E1.7 tags (spec 13 "Selection and tags"; E2's selection input).

PUT is wholesale replace (never merge), storage is normalized
(trim/dedupe/sort), validation rejects oversized and control-character tags,
filter-by-tag works on every entity list via the GIN-backed containment
operator, and the D35 scope rules carry over: viewers read, only
manage_devices writes, out-of-scope child items 404.
"""

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    Deployment,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "tags-owner@example.com"
VIEWER = "tags-viewer@example.com"
OP_A = "tags-op-a@example.com"

MAC = "02:E1:07:00:00:01"


@pytest.fixture(scope="module")
def tags_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate26-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="tags-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="tags-dep-a", slug="tags-dep-a")
        dep_b = Deployment(organization_id=org.id, name="tags-dep-b", slug="tags-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a = Pod(deployment_id=dep_a.id, name="tags-pod-a")
        pod_b = Pod(deployment_id=dep_b.id, name="tags-pod-b")
        db.add_all([pod_a, pod_b])
        db.flush()
        agg_a = Aggregator(pod_id=pod_a.id, aggregator_uuid="tags-agg-a")
        db.add(agg_a)
        db.flush()
        db.add(Listener(mac=MAC, name="tags-lst", aggregator_id=agg_a.id, deployment_id=dep_a.id))
        for email, role, scope in (
            (OWNER, "owner", None),
            (VIEWER, "viewer", None),
            (OP_A, "deployment_operator", dep_a.id),
        ):
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
        app.state.ids = {
            "org": str(org.id),
            "dep_a": str(dep_a.id),
            "dep_b": str(dep_b.id),
            "pod_a": str(pod_a.id),
            "pod_b": str(pod_b.id),
            "agg_a": str(agg_a.id),
        }
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def test_put_is_wholesale_replace_with_normalization(tags_app):
    owner = _login(tags_app, OWNER)
    path = f"{API_PREFIX}/listeners/{MAC}/tags"
    first = owner.put(
        path,
        json={"tags": ["  coastal ", "solar", "coastal", "", "  "]},
        headers=_csrf(owner),
    )
    assert first.status_code == 200
    assert first.json()["tags"] == ["coastal", "solar"]  # trimmed, deduped, sorted
    replaced = owner.put(path, json={"tags": ["ridge"]}, headers=_csrf(owner))
    assert replaced.json()["tags"] == ["ridge"]  # replace, never merge
    assert owner.get(path).json()["tags"] == ["ridge"]
    factory = tags_app.state.session_factory
    with factory() as db:
        detail = db.scalar(
            select(AuditLog.detail).where(
                AuditLog.action == "listener.update", AuditLog.entity_id == MAC
            )
        )
    assert detail == {"changed": ["tags"]}


def test_tag_validation_rejects_oversize_and_control_chars(tags_app):
    owner = _login(tags_app, OWNER)
    path = f"{API_PREFIX}/listeners/{MAC}/tags"
    too_long = owner.put(path, json={"tags": ["x" * 65]}, headers=_csrf(owner))
    assert too_long.status_code == 422
    control = owner.put(path, json={"tags": ["bad\ttag"]}, headers=_csrf(owner))
    assert control.status_code == 422


def test_every_entity_has_tag_endpoints_and_list_filters(tags_app):
    owner = _login(tags_app, OWNER)
    ids = tags_app.state.ids
    surfaces = [
        ("organizations", ids["org"], "name", "tags-org"),
        ("deployments", ids["dep_a"], "name", "tags-dep-a"),
        ("pods", ids["pod_a"], "name", "tags-pod-a"),
        ("aggregators", ids["agg_a"], "aggregator_uuid", "tags-agg-a"),
        ("listeners", MAC, "name", "tags-lst"),
    ]
    for entity, key, field, expected in surfaces:
        put = owner.put(
            f"{API_PREFIX}/{entity}/{key}/tags",
            json={"tags": [f"probe-{entity}"]},
            headers=_csrf(owner),
        )
        assert put.status_code == 200, (entity, put.text)
        # GIN-backed containment: the tagged row and only it comes back.
        hits = owner.get(f"{API_PREFIX}/{entity}", params={"tag": f"probe-{entity}"}).json()
        assert hits["total"] == 1, entity
        assert hits["items"][0][field] == expected
        misses = owner.get(f"{API_PREFIX}/{entity}", params={"tag": "no-such-tag"}).json()
        assert misses["total"] == 0, entity


def test_viewer_reads_tags_but_cannot_write(tags_app):
    viewer = _login(tags_app, VIEWER)
    ids = tags_app.state.ids
    read = viewer.get(f"{API_PREFIX}/pods/{ids['pod_a']}/tags")
    assert read.status_code == 200
    org_write = viewer.put(
        f"{API_PREFIX}/organizations/{ids['org']}/tags",
        json={"tags": ["nope"]},
        headers=_csrf(viewer),
    )
    assert org_write.status_code == 403  # org route keeps the explicit 403
    pod_write = viewer.put(
        f"{API_PREFIX}/pods/{ids['pod_a']}/tags", json={"tags": ["nope"]}, headers=_csrf(viewer)
    )
    assert pod_write.status_code == 404  # child rule: no manage_devices anywhere -> 404


def test_scoped_operator_hits_the_d35_boundary_on_tags(tags_app):
    op_a = _login(tags_app, OP_A)
    ids = tags_app.state.ids
    in_scope = op_a.put(
        f"{API_PREFIX}/pods/{ids['pod_a']}/tags", json={"tags": ["mine"]}, headers=_csrf(op_a)
    )
    assert in_scope.status_code == 200
    out_read = op_a.get(f"{API_PREFIX}/pods/{ids['pod_b']}/tags")
    assert out_read.status_code == 404
    out_write = op_a.put(
        f"{API_PREFIX}/pods/{ids['pod_b']}/tags", json={"tags": ["theirs"]}, headers=_csrf(op_a)
    )
    assert out_write.status_code == 404
    dep_read = op_a.get(f"{API_PREFIX}/deployments/{ids['dep_b']}/tags")
    assert dep_read.status_code == 403  # deployment routes keep the 403 pattern
