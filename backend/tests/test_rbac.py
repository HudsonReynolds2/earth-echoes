"""Gate 7: RBAC checks (task E0.7; spec 12.3).

TEST-CRITICAL (spec 14.5): this suite is the RBAC contract and doubles as its
documentation; no later session may weaken it (rule R0/R2). The table-driven
matrix is the phase-doc acceptance criterion; the endpoint half demonstrates
all four roles against protected routes, org-wide and deployment-scoped.
"""

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_auth import EMAIL, PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.auth.rbac import ROLE_PERMISSIONS, Permission, Role, has_permission, require_permission
from app.main import API_PREFIX, create_app
from app.models import RoleAssignment, User
from app.settings import Settings

DEPLOYMENT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
DEPLOYMENT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


def _assignment(role: Role, deployment_id: uuid.UUID | None = None) -> RoleAssignment:
    return RoleAssignment(user_id=uuid.uuid4(), role=role.value, deployment_id=deployment_id)


# --- the table-driven contract matrix (pure decision core) -----------------

# (role, assignment scope, permission, requested scope, expected)
MATRIX = [
    # Owner, org-wide: everything, everywhere.
    (Role.OWNER, None, Permission.MANAGE_USERS, None, True),
    (Role.OWNER, None, Permission.VIEW_AUDIT, None, True),
    (Role.OWNER, None, Permission.MANAGE_CONFIG, DEPLOYMENT_A, True),
    (Role.OWNER, None, Permission.VIEW_STATUS, DEPLOYMENT_B, True),
    # Deployment operator scoped to A: manages within A only, no org admin.
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.MANAGE_CONFIG, DEPLOYMENT_A, True),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.MANAGE_DEVICES, DEPLOYMENT_A, True),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.VIEW_TELEMETRY, DEPLOYMENT_A, True),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.MANAGE_CONFIG, DEPLOYMENT_B, False),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.MANAGE_CONFIG, None, False),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.MANAGE_USERS, None, False),
    (Role.DEPLOYMENT_OPERATOR, DEPLOYMENT_A, Permission.VIEW_AUDIT, None, False),
    # Field tech scoped to A: provisioning only; no config push, no telemetry.
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.MANAGE_PROVISIONING, DEPLOYMENT_A, True),
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.VIEW_PROVISIONING, DEPLOYMENT_A, True),
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.VIEW_STATUS, DEPLOYMENT_A, True),
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.MANAGE_CONFIG, DEPLOYMENT_A, False),
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.VIEW_TELEMETRY, DEPLOYMENT_A, False),
    (Role.FIELD_TECH, DEPLOYMENT_A, Permission.MANAGE_USERS, None, False),
    # Viewer, org-wide: read-only everywhere, never mutations.
    (Role.VIEWER, None, Permission.VIEW_STATUS, DEPLOYMENT_A, True),
    (Role.VIEWER, None, Permission.VIEW_TELEMETRY, DEPLOYMENT_B, True),
    (Role.VIEWER, None, Permission.VIEW_PROVISIONING, None, True),
    (Role.VIEWER, None, Permission.MANAGE_CONFIG, DEPLOYMENT_A, False),
    (Role.VIEWER, None, Permission.MANAGE_DEVICES, DEPLOYMENT_A, False),
    (Role.VIEWER, None, Permission.MANAGE_USERS, None, False),
]


@pytest.mark.parametrize(("role", "scope", "permission", "target", "expected"), MATRIX)
def test_permission_matrix(role, scope, permission, target, expected):
    assignments = [_assignment(role, scope)]
    assert has_permission(assignments, permission, target) is expected


def test_no_assignments_grants_nothing():
    for permission in Permission:
        assert has_permission([], permission, None) is False
        assert has_permission([], permission, DEPLOYMENT_A) is False


def test_multiple_assignments_union():
    assignments = [
        _assignment(Role.VIEWER, None),
        _assignment(Role.FIELD_TECH, DEPLOYMENT_A),
    ]
    assert has_permission(assignments, Permission.MANAGE_PROVISIONING, DEPLOYMENT_A) is True
    assert has_permission(assignments, Permission.MANAGE_PROVISIONING, DEPLOYMENT_B) is False
    assert has_permission(assignments, Permission.VIEW_STATUS, DEPLOYMENT_B) is True


def test_every_role_has_an_explicit_permission_set():
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)
    for role in (Role.DEPLOYMENT_OPERATOR, Role.FIELD_TECH, Role.VIEWER):
        assert Permission.MANAGE_USERS not in ROLE_PERMISSIONS[role], (
            f"{role}: org administration is owner-only (spec 12.3)"
        )


# --- all four roles against protected endpoints ----------------------------

ROLE_EMAILS = {
    Role.OWNER: "rbac-owner@example.com",
    Role.DEPLOYMENT_OPERATOR: "rbac-operator@example.com",
    Role.FIELD_TECH: "rbac-tech@example.com",
    Role.VIEWER: "rbac-viewer@example.com",
}


@pytest.fixture(scope="module")
def rbac_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate7-test-secret",
            kek="gate7-test-kek",
            cors_origins="",
        )
    )

    # Sample protected endpoints (phase-doc acceptance): org-level mutation
    # and a deployment-scoped read, mounted test-side like every _probe route
    # so no demo surface ships in production.
    @app.post(
        f"{API_PREFIX}/_probe/config",
        dependencies=[Depends(require_permission(Permission.MANAGE_CONFIG))],
    )
    def org_config() -> dict[str, bool]:
        return {"ok": True}

    @app.get(
        f"{API_PREFIX}/_probe/deployments/{{deployment_id}}/status",
        dependencies=[Depends(require_permission(Permission.VIEW_STATUS, "deployment_id"))],
    )
    def scoped_status(deployment_id: uuid.UUID) -> dict[str, bool]:
        return {"ok": True}

    factory = app.state.session_factory
    with factory() as db:
        for role, email in ROLE_EMAILS.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            scope = None if role in (Role.OWNER, Role.VIEWER) else DEPLOYMENT_A
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
            db.add(user)
        db.commit()
    return app


def _client_as(app: FastAPI, role: Role) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": ROLE_EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200
    return client


@pytest.mark.integration
def test_unauthenticated_requests_are_401(rbac_app):
    client = TestClient(rbac_app, raise_server_exceptions=False)
    assert client.post(f"{API_PREFIX}/_probe/config").status_code == 401
    assert client.get(f"{API_PREFIX}/_probe/deployments/{DEPLOYMENT_A}/status").status_code == 401


@pytest.mark.integration
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.OWNER, 200),
        (Role.DEPLOYMENT_OPERATOR, 403),
        (Role.FIELD_TECH, 403),
        (Role.VIEWER, 403),
    ],
)
def test_org_level_mutation_across_all_four_roles(rbac_app, role, expected):
    client = _client_as(rbac_app, role)
    response = client.post(f"{API_PREFIX}/_probe/config")
    assert response.status_code == expected, f"{role}: {response.text}"
    if expected == 403:
        assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("role", "in_scope", "out_of_scope"),
    [
        (Role.OWNER, 200, 200),
        (Role.DEPLOYMENT_OPERATOR, 200, 403),
        (Role.FIELD_TECH, 200, 403),
        (Role.VIEWER, 200, 200),
    ],
)
def test_scoped_read_across_all_four_roles(rbac_app, role, in_scope, out_of_scope):
    client = _client_as(rbac_app, role)
    assert (
        client.get(f"{API_PREFIX}/_probe/deployments/{DEPLOYMENT_A}/status").status_code == in_scope
    )
    assert (
        client.get(f"{API_PREFIX}/_probe/deployments/{DEPLOYMENT_B}/status").status_code
        == out_of_scope
    )


@pytest.mark.integration
def test_me_reports_assignments(rbac_app):
    client = _client_as(rbac_app, Role.DEPLOYMENT_OPERATOR)
    body = client.get(f"{API_PREFIX}/auth/me").json()
    assert body["assignments"] == [
        {"role": "deployment_operator", "deployment_id": str(DEPLOYMENT_A)}
    ]


@pytest.mark.integration
def test_invalid_scope_uuid_is_a_validation_error(rbac_app):
    client = _client_as(rbac_app, Role.OWNER)
    response = client.get(f"{API_PREFIX}/_probe/deployments/not-a-uuid/status")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
