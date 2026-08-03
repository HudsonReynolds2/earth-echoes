"""Gate 21: E1.2 CRUD surface (spec 13; DECISIONS D34-D36).

Per-entity happy paths, the D7 envelope and sort grammar, filters, the
single-org clamp and absent org DELETE (D34), slug generation and the D36
freeze, MAC normalization at the boundary and in paths, delete-with-children
409s including the D33 role-assignment blocker, PATCH parent-field rejection
(D32), and the audit trail behind every mutation.
"""

import uuid

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import AuditLog, Organization, RoleAssignment, User
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "crud-owner@example.com"
VIEWER = "crud-viewer@example.com"


@pytest.fixture(scope="module")
def crud_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate21-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="crud-org")
        db.add(org)
        db.flush()
        app.state.org_id = org.id
        for email, role in ((OWNER, "owner"), (VIEWER, "viewer")):
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=None))
            db.add(user)
        db.commit()
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def _create_deployment(client, app, name, **extra):
    return client.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": str(app.state.org_id), "name": name, **extra},
        headers=_csrf(client),
    )


# --- organizations (D34) -----------------------------------------------------


def test_single_org_clamp_and_no_delete_route(crud_app):
    owner = _login(crud_app, OWNER)
    second = owner.post(
        f"{API_PREFIX}/organizations", json={"name": "second-org"}, headers=_csrf(owner)
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    # Spec 13 lists no DELETE for organizations (D34, project-changes #13).
    gone = owner.delete(f"{API_PREFIX}/organizations/{crud_app.state.org_id}", headers=_csrf(owner))
    assert gone.status_code == 405


def test_org_readable_by_viewer_and_patchable_by_owner(crud_app):
    viewer = _login(crud_app, VIEWER)
    listed = viewer.get(f"{API_PREFIX}/organizations")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    owner = _login(crud_app, OWNER)
    patched = owner.patch(
        f"{API_PREFIX}/organizations/{crud_app.state.org_id}",
        json={"name": "crud-org-renamed"},
        headers=_csrf(owner),
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "crud-org-renamed"
    # Viewer holds VIEW_STATUS, not MANAGE_DEVICES: writes are forbidden.
    denied = viewer.patch(
        f"{API_PREFIX}/organizations/{crud_app.state.org_id}",
        json={"name": "nope"},
        headers=_csrf(viewer),
    )
    assert denied.status_code == 403


# --- deployments: slug lifecycle (D36) ---------------------------------------


def test_deployment_create_generates_slug_and_freezes_it_after_first_pod(crud_app):
    owner = _login(crud_app, OWNER)
    created = _create_deployment(owner, crud_app, "Redwood Coast")
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "redwood-coast"
    assert body["pod_count"] == 0 and body["listener_count"] == 0
    dep_id = body["id"]

    # Slug is editable before first use...
    moved = owner.patch(
        f"{API_PREFIX}/deployments/{dep_id}", json={"slug": "redwood-1"}, headers=_csrf(owner)
    )
    assert moved.status_code == 200 and moved.json()["slug"] == "redwood-1"

    # ... and frozen once a pod exists (D36).
    pod = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": dep_id, "name": "Pod 01"},
        headers=_csrf(owner),
    )
    assert pod.status_code == 201
    frozen = owner.patch(
        f"{API_PREFIX}/deployments/{dep_id}", json={"slug": "renamed"}, headers=_csrf(owner)
    )
    assert frozen.status_code == 409
    assert "frozen" in frozen.json()["error"]["message"]
    # Renaming the display name stays legal; a same-slug PATCH is a no-op.
    renamed = owner.patch(
        f"{API_PREFIX}/deployments/{dep_id}",
        json={"name": "Redwood Coast North", "slug": "redwood-1"},
        headers=_csrf(owner),
    )
    assert renamed.status_code == 200


def test_deployment_duplicate_name_conflicts_and_slug_collision_suffixes(crud_app):
    owner = _login(crud_app, OWNER)
    assert _create_deployment(owner, crud_app, "Twin Peak").status_code == 201
    dup = _create_deployment(owner, crud_app, "Twin Peak")
    assert dup.status_code == 409
    # A different name that slugifies onto a taken slug gets the -2 suffix.
    suffixed = _create_deployment(owner, crud_app, "Twin  Peak!")
    assert suffixed.status_code == 201
    assert suffixed.json()["slug"] == "twin-peak-2"


def test_deployment_delete_blocks_on_pods_and_role_assignments(crud_app):
    owner = _login(crud_app, OWNER)
    dep_id = _create_deployment(owner, crud_app, "Blocked Delete").json()["id"]
    pod_id = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": dep_id, "name": "blocker-pod"},
        headers=_csrf(owner),
    ).json()["id"]

    blocked = owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["children"] == {"pods": 1}

    assert owner.delete(f"{API_PREFIX}/pods/{pod_id}", headers=_csrf(owner)).status_code == 204

    # A scoped role assignment blocks too (D33): deleting it implicitly would
    # change access as a side effect of an inventory call.
    factory = crud_app.state.session_factory
    with factory() as db:
        user = User(email="crud-scoped@example.com", password_hash=hash_password(PASSWORD))
        user.role_assignments.append(
            RoleAssignment(role="field_tech", deployment_id=uuid.UUID(dep_id))
        )
        db.add(user)
        db.commit()
    blocked_again = owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner))
    assert blocked_again.status_code == 409
    assert blocked_again.json()["error"]["detail"]["children"] == {"role_assignments": 1}
    with factory() as db:
        db.execute(
            RoleAssignment.__table__.delete().where(
                RoleAssignment.deployment_id == uuid.UUID(dep_id)
            )
        )
        db.commit()
    assert (
        owner.delete(f"{API_PREFIX}/deployments/{dep_id}", headers=_csrf(owner)).status_code == 204
    )


def test_deployment_list_envelope_sort_and_filters(crud_app):
    owner = _login(crud_app, OWNER)
    listed = owner.get(f"{API_PREFIX}/deployments", params={"sort": "-name", "limit": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    names = [item["name"] for item in body["items"]]
    assert names == sorted(names, reverse=True)[: len(names)]
    bad_sort = owner.get(f"{API_PREFIX}/deployments", params={"sort": "bogus"})
    assert bad_sort.status_code == 422
    assert "allowed" in bad_sort.json()["error"]["detail"]
    filtered = owner.get(f"{API_PREFIX}/deployments", params={"name": "twin"})
    assert {item["name"] for item in filtered.json()["items"]} == {"Twin Peak", "Twin  Peak!"}
    assert owner.get(f"{API_PREFIX}/deployments", params={"tag": "nope"}).json()["total"] == 0


# --- pods / aggregators / listeners ------------------------------------------


@pytest.fixture(scope="module")
def tree(crud_app):
    """A deployment with two pods, each with an aggregator, plus a second
    deployment for cross-deployment assertions."""
    owner = _login(crud_app, OWNER)
    dep_a = _create_deployment(owner, crud_app, "Tree Alpha").json()["id"]
    dep_b = _create_deployment(owner, crud_app, "Tree Beta").json()["id"]
    pods = {}
    aggs = {}
    for key, dep, pod_name in (
        ("a1", dep_a, "Pod A1"),
        ("a2", dep_a, "Pod A2"),
        ("b1", dep_b, "Pod B1"),
    ):
        pods[key] = owner.post(
            f"{API_PREFIX}/pods",
            json={"deployment_id": dep, "name": pod_name},
            headers=_csrf(owner),
        ).json()["id"]
        aggs[key] = owner.post(
            f"{API_PREFIX}/aggregators",
            json={"pod_id": pods[key], "aggregator_uuid": f"tree-agg-{key}"},
            headers=_csrf(owner),
        ).json()
    return {"dep_a": dep_a, "dep_b": dep_b, "pods": pods, "aggs": aggs}


def test_pod_crud_and_duplicate_name(crud_app, tree):
    owner = _login(crud_app, OWNER)
    dup = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": tree["dep_a"], "name": "Pod A1"},
        headers=_csrf(owner),
    )
    assert dup.status_code == 409
    fetched = owner.get(f"{API_PREFIX}/pods/{tree['pods']['a1']}")
    assert fetched.status_code == 200
    assert fetched.json()["aggregator"]["aggregator_uuid"] == "tree-agg-a1"
    renamed = owner.patch(
        f"{API_PREFIX}/pods/{tree['pods']['a1']}",
        json={"name": "Pod A1 Renamed"},
        headers=_csrf(owner),
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Pod A1 Renamed"
    blocked = owner.delete(f"{API_PREFIX}/pods/{tree['pods']['a1']}", headers=_csrf(owner))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["children"] == {"aggregators": 1}


def test_aggregator_occupied_pod_and_duplicate_uuid_conflict(crud_app, tree):
    owner = _login(crud_app, OWNER)
    squatter = owner.post(
        f"{API_PREFIX}/aggregators",
        json={"pod_id": tree["pods"]["a1"]},
        headers=_csrf(owner),
    )
    assert squatter.status_code == 409
    pod = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": tree["dep_b"], "name": "Pod B2"},
        headers=_csrf(owner),
    ).json()["id"]
    dup_uuid = owner.post(
        f"{API_PREFIX}/aggregators",
        json={"pod_id": pod, "aggregator_uuid": "tree-agg-a1"},
        headers=_csrf(owner),
    )
    assert dup_uuid.status_code == 409
    generated = owner.post(f"{API_PREFIX}/aggregators", json={"pod_id": pod}, headers=_csrf(owner))
    assert generated.status_code == 201
    assert len(generated.json()["aggregator_uuid"]) == 32  # platform-assigned uuid4().hex


def test_listener_mac_normalization_create_and_paths(crud_app, tree):
    owner = _login(crud_app, OWNER)
    created = owner.post(
        f"{API_PREFIX}/listeners",
        json={
            "mac": "aa-bb-cc-dd-ee-01",
            "name": "alder-01",
            "aggregator_id": tree["aggs"]["a1"]["id"],
            "gps_lat": 47.6412,
            "gps_lon": -121.8871,
        },
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["mac"] == "AA:BB:CC:DD:EE:01"
    assert body["deployment_id"] == tree["dep_a"]  # the D32 stamp, server-computed

    # Any separator convention resolves to the same device in paths.
    for variant in ("aa-bb-cc-dd-ee-01", "AA:BB:CC:DD:EE:01", "aabb.ccdd.ee01"):
        fetched = owner.get(f"{API_PREFIX}/listeners/{variant}")
        assert fetched.status_code == 200 and fetched.json()["mac"] == "AA:BB:CC:DD:EE:01"

    dup_mac = owner.post(
        f"{API_PREFIX}/listeners",
        json={
            "mac": "AA:BB:CC:DD:EE:01",
            "name": "other-name",
            "aggregator_id": tree["aggs"]["b1"]["id"],
        },
        headers=_csrf(owner),
    )
    assert dup_mac.status_code == 409

    bad_mac = owner.post(
        f"{API_PREFIX}/listeners",
        json={"mac": "not-a-mac", "name": "x", "aggregator_id": tree["aggs"]["a1"]["id"]},
        headers=_csrf(owner),
    )
    assert bad_mac.status_code == 422


def test_listener_name_unique_within_deployment_only(crud_app, tree):
    owner = _login(crud_app, OWNER)
    base = {
        "name": "shared-name",
        "gps_lat": None,
        "gps_lon": None,
    }
    first = owner.post(
        f"{API_PREFIX}/listeners",
        json={**base, "mac": "AA:BB:CC:DD:EE:02", "aggregator_id": tree["aggs"]["a1"]["id"]},
        headers=_csrf(owner),
    )
    assert first.status_code == 201
    # Same deployment, different pod: rejected.
    same_dep = owner.post(
        f"{API_PREFIX}/listeners",
        json={**base, "mac": "AA:BB:CC:DD:EE:03", "aggregator_id": tree["aggs"]["a2"]["id"]},
        headers=_csrf(owner),
    )
    assert same_dep.status_code == 409
    # Other deployment: fine.
    other_dep = owner.post(
        f"{API_PREFIX}/listeners",
        json={**base, "mac": "AA:BB:CC:DD:EE:04", "aggregator_id": tree["aggs"]["b1"]["id"]},
        headers=_csrf(owner),
    )
    assert other_dep.status_code == 201


def test_listener_patch_rejects_parent_and_mac_fields(crud_app, tree):
    owner = _login(crud_app, OWNER)
    for payload in (
        {"mac": "AA:BB:CC:DD:EE:99"},
        {"aggregator_id": tree["aggs"]["a2"]["id"]},
        {"deployment_id": tree["dep_b"]},
    ):
        response = owner.patch(
            f"{API_PREFIX}/listeners/AA:BB:CC:DD:EE:01", json=payload, headers=_csrf(owner)
        )
        assert response.status_code == 422, payload
    moved = owner.patch(
        f"{API_PREFIX}/listeners/AA:BB:CC:DD:EE:01",
        json={"name": "alder-01-renamed", "gps_lat": None, "gps_lon": None},
        headers=_csrf(owner),
    )
    assert moved.status_code == 200
    assert moved.json()["gps_lat"] is None
    out_of_range = owner.patch(
        f"{API_PREFIX}/listeners/AA:BB:CC:DD:EE:01", json={"gps_lat": 91}, headers=_csrf(owner)
    )
    assert out_of_range.status_code == 422


def test_create_bodies_reject_the_stamp_and_unknown_fields(crud_app, tree):
    owner = _login(crud_app, OWNER)
    smuggled = owner.post(
        f"{API_PREFIX}/listeners",
        json={
            "mac": "AA:BB:CC:DD:EE:05",
            "name": "smuggler",
            "aggregator_id": tree["aggs"]["a1"]["id"],
            "deployment_id": tree["dep_b"],  # D32: the stamp is never client-supplied
        },
        headers=_csrf(owner),
    )
    assert smuggled.status_code == 422


def test_mutations_require_csrf_and_write_audit_rows(crud_app, tree):
    owner = _login(crud_app, OWNER)
    naked = owner.post(
        f"{API_PREFIX}/pods", json={"deployment_id": tree["dep_a"], "name": "no-csrf"}
    )
    assert naked.status_code == 403
    factory = crud_app.state.session_factory
    with factory() as db:
        actions = set(
            db.scalars(
                select(AuditLog.action).where(
                    AuditLog.action.in_(
                        ["deployment.create", "pod.create", "aggregator.create", "listener.create"]
                    )
                )
            ).all()
        )
    assert actions == {
        "deployment.create",
        "pod.create",
        "aggregator.create",
        "listener.create",
    }
    with factory() as db:
        scoped = db.scalar(
            select(AuditLog.scope).where(AuditLog.action == "listener.create").limit(1)
        )
    assert scoped is not None  # every hierarchy mutation carries its deployment scope
