"""Gate 21: E1.2 deployment-scoped visibility (DECISIONS D35).

Table-driven over (user, endpoint): who sees which rows in every hierarchy
list, and the 403/404 asymmetry on item routes - /deployments/{id} keeps the
E0.7 403-before-lookup pattern; child items (pod/aggregator/listener) answer
an identical 404 whether the row is out of scope or nonexistent, so an
enumerable MAC never becomes an existence oracle.

This suite is deliberately separate from test_rbac.py: that file is the
locked permission-matrix contract (test-critical); this one covers the list
filtering and resolution the matrix never modeled.
"""

import uuid

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import Aggregator, Deployment, Listener, Organization, Pod, RoleAssignment, User
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "scope-owner@example.com"
ORG_VIEWER = "scope-viewer@example.com"
OP_A = "scope-op-a@example.com"
TECH_B = "scope-tech-b@example.com"
NO_GRANTS = "scope-none@example.com"

MAC_A = "02:5C:0E:00:00:0A"
MAC_B = "02:5C:0E:00:00:0B"


@pytest.fixture(scope="module")
def scope_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate21-scope-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="scope-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="scope-dep-a", slug="scope-dep-a")
        dep_b = Deployment(organization_id=org.id, name="scope-dep-b", slug="scope-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a = Pod(deployment_id=dep_a.id, name="scope-pod-a")
        pod_b = Pod(deployment_id=dep_b.id, name="scope-pod-b")
        db.add_all([pod_a, pod_b])
        db.flush()
        agg_a = Aggregator(pod_id=pod_a.id, aggregator_uuid="scope-agg-a")
        agg_b = Aggregator(pod_id=pod_b.id, aggregator_uuid="scope-agg-b")
        db.add_all([agg_a, agg_b])
        db.flush()
        db.add_all(
            [
                Listener(
                    mac=MAC_A, name="scope-lst-a", aggregator_id=agg_a.id, deployment_id=dep_a.id
                ),
                Listener(
                    mac=MAC_B, name="scope-lst-b", aggregator_id=agg_b.id, deployment_id=dep_b.id
                ),
            ]
        )
        grants = {
            OWNER: ("owner", None),
            ORG_VIEWER: ("viewer", None),
            OP_A: ("deployment_operator", dep_a.id),
            TECH_B: ("field_tech", dep_b.id),
        }
        for email, (role, scope) in grants.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.add(User(email=NO_GRANTS, password_hash=hash_password(PASSWORD)))
        db.commit()
        app.state.ids = {
            "dep_a": str(dep_a.id),
            "dep_b": str(dep_b.id),
            "pod_a": str(pod_a.id),
            "pod_b": str(pod_b.id),
            "agg_a": str(agg_a.id),
            "agg_b": str(agg_b.id),
        }
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


# --- list visibility, table-driven -------------------------------------------

# (user, expected marker set) per list endpoint; markers name fixture rows.
LIST_CASES = [
    (OWNER, {"a", "b"}),
    (ORG_VIEWER, {"a", "b"}),
    (OP_A, {"a"}),
    (TECH_B, {"b"}),
]


@pytest.mark.parametrize(("email", "expected"), LIST_CASES)
def test_deployment_list_visibility(scope_app, email, expected):
    client = _login(scope_app, email)
    names = {item["name"] for item in client.get(f"{API_PREFIX}/deployments").json()["items"]}
    assert names == {f"scope-dep-{marker}" for marker in expected}


@pytest.mark.parametrize(("email", "expected"), LIST_CASES)
def test_pod_list_visibility(scope_app, email, expected):
    client = _login(scope_app, email)
    names = {item["name"] for item in client.get(f"{API_PREFIX}/pods").json()["items"]}
    assert names == {f"scope-pod-{marker}" for marker in expected}


@pytest.mark.parametrize(("email", "expected"), LIST_CASES)
def test_aggregator_list_visibility(scope_app, email, expected):
    client = _login(scope_app, email)
    uuids = {
        item["aggregator_uuid"] for item in client.get(f"{API_PREFIX}/aggregators").json()["items"]
    }
    assert uuids == {f"scope-agg-{marker}" for marker in expected}


@pytest.mark.parametrize(("email", "expected"), LIST_CASES)
def test_listener_list_visibility(scope_app, email, expected):
    client = _login(scope_app, email)
    names = {item["name"] for item in client.get(f"{API_PREFIX}/listeners").json()["items"]}
    assert names == {f"scope-lst-{marker}" for marker in expected}


def test_no_grants_means_403_on_every_hierarchy_surface(scope_app):
    client = _login(scope_app, NO_GRANTS)
    for path in ("/organizations", "/deployments", "/pods", "/aggregators", "/listeners"):
        response = client.get(f"{API_PREFIX}{path}")
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"


# --- the 403/404 asymmetry (D35) ----------------------------------------------


def test_deployment_item_keeps_the_403_pattern(scope_app):
    op_a = _login(scope_app, OP_A)
    ids = scope_app.state.ids
    assert op_a.get(f"{API_PREFIX}/deployments/{ids['dep_a']}").status_code == 200
    denied = op_a.get(f"{API_PREFIX}/deployments/{ids['dep_b']}")
    assert denied.status_code == 403  # check runs before lookup; confirms nothing


def test_child_items_answer_the_same_404_for_out_of_scope_and_missing(scope_app):
    op_a = _login(scope_app, OP_A)
    ids = scope_app.state.ids
    in_scope = op_a.get(f"{API_PREFIX}/pods/{ids['pod_a']}")
    assert in_scope.status_code == 200

    out_of_scope = op_a.get(f"{API_PREFIX}/pods/{ids['pod_b']}")
    missing = op_a.get(f"{API_PREFIX}/pods/{uuid.uuid4()}")
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.json()["error"] == missing.json()["error"]  # indistinguishable

    # The MAC oracle case, verbatim: an existing-but-foreign listener and a
    # nonexistent one are the same 404 body.
    foreign = op_a.get(f"{API_PREFIX}/listeners/{MAC_B}")
    ghost = op_a.get(f"{API_PREFIX}/listeners/02:5C:0E:FF:FF:FF")
    assert foreign.status_code == ghost.status_code == 404
    assert foreign.json()["error"] == ghost.json()["error"]

    out_agg = op_a.get(f"{API_PREFIX}/aggregators/{ids['agg_b']}")
    assert out_agg.status_code == 404


def test_out_of_scope_writes_are_404_for_items_403_for_posts(scope_app):
    op_a = _login(scope_app, OP_A)
    ids = scope_app.state.ids
    patched = op_a.patch(
        f"{API_PREFIX}/pods/{ids['pod_b']}", json={"name": "hijack"}, headers=_csrf(op_a)
    )
    assert patched.status_code == 404
    deleted = op_a.delete(f"{API_PREFIX}/listeners/{MAC_B}", headers=_csrf(op_a))
    assert deleted.status_code == 404
    # POST carries the parent in the body - the client asserted the scope, so
    # denial is a plain 403 and confirms nothing (D35).
    posted = op_a.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": ids["dep_b"], "name": "trespass"},
        headers=_csrf(op_a),
    )
    assert posted.status_code == 403


def test_write_permissions_follow_the_matrix(scope_app):
    ids = scope_app.state.ids
    op_a = _login(scope_app, OP_A)
    created = op_a.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": ids["dep_a"], "name": "op-a-pod"},
        headers=_csrf(op_a),
    )
    assert created.status_code == 201
    # Deployment creation is an org-level write; a scoped operator lacks it.
    dep = op_a.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": "00000000-0000-0000-0000-000000000000", "name": "nope"},
        headers=_csrf(op_a),
    )
    assert dep.status_code == 403
    # A field tech never holds manage_devices, even in scope.
    tech_b = _login(scope_app, TECH_B)
    denied = tech_b.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": ids["dep_b"], "name": "tech-pod"},
        headers=_csrf(tech_b),
    )
    assert denied.status_code == 403
    # An org-wide viewer reads everything and writes nothing.
    viewer = _login(scope_app, ORG_VIEWER)
    read = viewer.get(f"{API_PREFIX}/pods/{ids['pod_b']}")
    assert read.status_code == 200
    write = viewer.patch(
        f"{API_PREFIX}/pods/{ids['pod_b']}", json={"name": "viewer-write"}, headers=_csrf(viewer)
    )
    assert write.status_code == 404  # out of MANAGE_DEVICES scope entirely -> same 404 rule
