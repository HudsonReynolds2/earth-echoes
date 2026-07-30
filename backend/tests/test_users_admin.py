"""Gate 9: user administration (task E0.9; spec 13).

Phase-doc acceptance: an owner creates a viewer, the viewer logs in and sees
read-only access. Every E0 mechanism converges on this surface: RBAC gate,
CSRF on mutations, audit rows, the D7 list contract, D1 session revocation on
deactivation, and the self-lockout guard.
"""

import uuid

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import AuditLog, RoleAssignment, User
from app.settings import Settings

OWNER_EMAIL = "admin-owner@example.com"


@pytest.fixture(scope="module")
def admin_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate9-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        owner = User(email=OWNER_EMAIL, password_hash=hash_password(PASSWORD))
        owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        db.add(owner)
        db.commit()
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def _create_user(client: TestClient, email: str, role: str = "viewer", **overrides):
    payload = {
        "email": email,
        "password": PASSWORD,
        "assignments": [{"role": role, "deployment_id": None}],
        **overrides,
    }
    return client.post(f"{API_PREFIX}/users", json=payload, headers=_csrf(client))


# --- acceptance flow: owner creates a viewer, viewer is read-only ----------


@pytest.mark.integration
def test_owner_creates_viewer_who_logs_in_and_is_read_only(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    created = _create_user(owner, "flow-viewer@example.com")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["assignments"] == [{"role": "viewer", "deployment_id": None}]
    assert "password" not in created.text and "hash" not in body

    viewer = _login(admin_app, "flow-viewer@example.com")
    me = viewer.get(f"{API_PREFIX}/auth/me").json()
    assert me["assignments"][0]["role"] == "viewer"
    # Read-only in practice: administration and its mutations are forbidden.
    assert viewer.get(f"{API_PREFIX}/users").status_code == 403
    assert viewer.post(f"{API_PREFIX}/users", json={}, headers=_csrf(viewer)).status_code == 403


# --- RBAC and CSRF on the surface ------------------------------------------


@pytest.mark.integration
def test_users_surface_requires_owner_and_csrf(admin_app):
    anonymous = TestClient(admin_app, raise_server_exceptions=False)
    assert anonymous.get(f"{API_PREFIX}/users").status_code == 401

    owner = _login(admin_app, OWNER_EMAIL)
    no_csrf = owner.post(
        f"{API_PREFIX}/users",
        json={"email": "x@example.com", "password": PASSWORD, "assignments": []},
    )
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "forbidden"


# --- create paths -----------------------------------------------------------


@pytest.mark.integration
def test_duplicate_email_conflicts(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    assert _create_user(owner, "dupe@example.com").status_code == 201
    duplicate = _create_user(owner, "dupe@example.com")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


@pytest.mark.integration
def test_unknown_role_is_a_validation_error(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    response = _create_user(owner, "badrole@example.com", role="superuser")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.integration
def test_create_writes_an_audit_row_without_secrets(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    assert _create_user(owner, "audited@example.com").status_code == 201
    factory = admin_app.state.session_factory
    with factory() as db:
        row = db.scalar(
            select(AuditLog).where(AuditLog.action == "user.create").order_by(AuditLog.at.desc())
        )
    assert row is not None
    assert row.detail is not None and row.detail["email"] == "audited@example.com"
    assert PASSWORD not in str(row.detail), "audit detail leaked a password"


# --- update paths -----------------------------------------------------------


@pytest.mark.integration
def test_deactivation_revokes_live_sessions_immediately(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    assert _create_user(owner, "doomed@example.com").status_code == 201
    victim = _login(admin_app, "doomed@example.com")
    assert victim.get(f"{API_PREFIX}/auth/me").status_code == 200

    factory = admin_app.state.session_factory
    with factory() as db:
        victim_id = db.scalar(select(User.id).where(User.email == "doomed@example.com"))
    response = owner.patch(
        f"{API_PREFIX}/users/{victim_id}", json={"is_active": False}, headers=_csrf(owner)
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    # D1: the victim's live session is dead, not just future logins.
    assert victim.get(f"{API_PREFIX}/auth/me").status_code == 401
    relogin = TestClient(admin_app, raise_server_exceptions=False).post(
        f"{API_PREFIX}/auth/login",
        json={"email": "doomed@example.com", "password": PASSWORD},
    )
    assert relogin.status_code == 401


@pytest.mark.integration
def test_password_change_takes_effect(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    assert _create_user(owner, "rotate@example.com").status_code == 201
    factory = admin_app.state.session_factory
    with factory() as db:
        target_id = db.scalar(select(User.id).where(User.email == "rotate@example.com"))
    new_password = f"pw-{uuid.uuid4().hex}"
    assert (
        owner.patch(
            f"{API_PREFIX}/users/{target_id}",
            json={"password": new_password},
            headers=_csrf(owner),
        ).status_code
        == 200
    )
    fresh = TestClient(admin_app, raise_server_exceptions=False)
    old = fresh.post(
        f"{API_PREFIX}/auth/login", json={"email": "rotate@example.com", "password": PASSWORD}
    )
    assert old.status_code == 401
    new = fresh.post(
        f"{API_PREFIX}/auth/login",
        json={"email": "rotate@example.com", "password": new_password},
    )
    assert new.status_code == 200


@pytest.mark.integration
def test_assignments_replace_wholesale(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    assert _create_user(owner, "reroles@example.com").status_code == 201
    factory = admin_app.state.session_factory
    with factory() as db:
        target_id = db.scalar(select(User.id).where(User.email == "reroles@example.com"))
    deployment = str(uuid.uuid4())
    response = owner.patch(
        f"{API_PREFIX}/users/{target_id}",
        json={"assignments": [{"role": "field_tech", "deployment_id": deployment}]},
        headers=_csrf(owner),
    )
    assert response.status_code == 200
    assert response.json()["assignments"] == [{"role": "field_tech", "deployment_id": deployment}]


@pytest.mark.integration
def test_self_lockout_is_blocked(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    factory = admin_app.state.session_factory
    with factory() as db:
        owner_id = db.scalar(select(User.id).where(User.email == OWNER_EMAIL))
    deactivate = owner.patch(
        f"{API_PREFIX}/users/{owner_id}", json={"is_active": False}, headers=_csrf(owner)
    )
    assert deactivate.status_code == 409
    demote = owner.patch(
        f"{API_PREFIX}/users/{owner_id}",
        json={"assignments": [{"role": "viewer", "deployment_id": None}]},
        headers=_csrf(owner),
    )
    assert demote.status_code == 409


@pytest.mark.integration
def test_unknown_user_is_404_and_list_follows_d7(admin_app):
    owner = _login(admin_app, OWNER_EMAIL)
    missing = owner.patch(
        f"{API_PREFIX}/users/{uuid.uuid4()}", json={"is_active": False}, headers=_csrf(owner)
    )
    assert missing.status_code == 404
    listing = owner.get(f"{API_PREFIX}/users", params={"email": "flow-viewer", "limit": 5})
    body = listing.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 1
    assert body["items"][0]["email"] == "flow-viewer@example.com"
