"""Gate 8: audit log (task E0.8; spec 14.1, 13; addendum PHASE0-4-02).

Phase-doc acceptance: every mutating endpoint in this phase writes an audit
row; the table has no update or delete path in application code. Plus the D3
database-layer revoke, atomicity with the mutation's transaction, request-id
correlation, and the filterable owner-gated read surface.
"""

import uuid

import pytest
from conftest import REPO_ROOT
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.audit import record_audit
from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import AuditLog, RoleAssignment, User
from app.settings import Settings

OWNER_EMAIL = "audit-owner@example.com"
VIEWER_EMAIL = "audit-viewer@example.com"
SCOPE_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")


@pytest.fixture(scope="module")
def audit_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate8-test-secret",
            kek="gate8-test-kek",
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        owner = User(email=OWNER_EMAIL, password_hash=hash_password(PASSWORD))
        owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        viewer = User(email=VIEWER_EMAIL, password_hash=hash_password(PASSWORD))
        viewer.role_assignments.append(RoleAssignment(role="viewer", deployment_id=None))
        db.add_all([owner, viewer])
        db.commit()
    return app


def _login(app, email: str, request_id: str | None = None) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Request-ID": request_id} if request_id else {}
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
    )
    assert response.status_code == 200
    return client


def _rows(app, **filters) -> list[AuditLog]:
    factory = app.state.session_factory
    with factory() as db:
        statement = select(AuditLog)
        for key, value in filters.items():
            statement = statement.where(getattr(AuditLog, key) == value)
        return list(db.scalars(statement.order_by(AuditLog.at.desc())))


# --- every mutation in this phase audits -----------------------------------


@pytest.mark.integration
def test_login_writes_an_audit_row_with_the_request_id(audit_app):
    _login(audit_app, OWNER_EMAIL, request_id="audit-req-77")
    rows = _rows(audit_app, action="auth.login", request_id="audit-req-77")
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "user"
    assert row.actor_user_id is not None
    assert str(row.actor_user_id) == row.entity_id


@pytest.mark.integration
def test_logout_writes_an_audit_row(audit_app):
    client = _login(audit_app, OWNER_EMAIL)
    csrf = client.cookies["eoe_csrf"]
    before = len(_rows(audit_app, action="auth.logout"))
    assert (
        client.post(f"{API_PREFIX}/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    )
    assert len(_rows(audit_app, action="auth.logout")) == before + 1


# --- atomicity: the audit row rides the mutation's transaction -------------


@pytest.mark.integration
def test_rolled_back_transaction_discards_the_audit_row(audit_app):
    factory = audit_app.state.session_factory
    before = len(_rows(audit_app, action="probe.rollback"))
    with factory() as db:
        record_audit(db, action="probe.rollback", entity_type="probe", entity_id="x")
        db.rollback()
    assert len(_rows(audit_app, action="probe.rollback")) == before


# --- immutability ----------------------------------------------------------


def test_migration_revokes_update_and_delete():
    migration = next((REPO_ROOT / "backend" / "alembic" / "versions").glob("*audit_log.py"))
    text = migration.read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE ON TABLE audit_log FROM PUBLIC" in text
    assert "GRANT UPDATE, DELETE ON TABLE audit_log TO PUBLIC" in text  # reversible


def test_no_mutating_audit_routes_exist(audit_app):
    for route in audit_app.routes:
        path = getattr(route, "path", "") or ""
        methods = getattr(route, "methods", None) or set()
        if "audit" in path:
            assert methods <= {"GET", "HEAD"}, f"mutating audit route: {methods} {path}"


# --- the read surface ------------------------------------------------------


@pytest.mark.integration
def test_audit_read_is_owner_gated(audit_app):
    anonymous = TestClient(audit_app, raise_server_exceptions=False)
    assert anonymous.get(f"{API_PREFIX}/audit").status_code == 401
    viewer = _login(audit_app, VIEWER_EMAIL)
    assert viewer.get(f"{API_PREFIX}/audit").status_code == 403
    owner = _login(audit_app, OWNER_EMAIL)
    response = owner.get(f"{API_PREFIX}/audit")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}  # D7 envelope


@pytest.mark.integration
def test_audit_filters_by_action_actor_and_scope(audit_app):
    owner = _login(audit_app, OWNER_EMAIL)
    factory = audit_app.state.session_factory
    actor_id = uuid.uuid4()
    with factory() as db:
        # Committed first: audit_log has a plain FK to user with no ORM
        # relationship, so the unit of work will not order the inserts.
        probe_user = User(email=f"probe-{actor_id.hex[:8]}@example.com", password_hash="x")
        probe_user.id = actor_id
        db.add(probe_user)
        db.commit()
    with factory() as db:
        record_audit(
            db,
            action="probe.scoped",
            entity_type="probe",
            entity_id="p1",
            actor_user_id=actor_id,
            scope=SCOPE_A,
            detail={"kind": "filter-check"},
        )
        db.commit()

    by_action = owner.get(f"{API_PREFIX}/audit", params={"action": "probe.scoped"}).json()
    assert by_action["total"] == 1
    entry = by_action["items"][0]
    assert entry["scope"] == str(SCOPE_A)
    assert entry["detail"] == {"kind": "filter-check"}

    by_actor = owner.get(f"{API_PREFIX}/audit", params={"actor": str(actor_id)}).json()
    assert by_actor["total"] == 1
    assert by_actor["items"][0]["action"] == "probe.scoped"

    by_scope = owner.get(f"{API_PREFIX}/audit", params={"scope": str(SCOPE_A)}).json()
    assert by_scope["total"] == 1

    none = owner.get(f"{API_PREFIX}/audit", params={"scope": str(uuid.uuid4())}).json()
    assert none["total"] == 0


@pytest.mark.integration
def test_audit_defaults_to_newest_first_and_paginates(audit_app):
    owner = _login(audit_app, OWNER_EMAIL)
    body = owner.get(f"{API_PREFIX}/audit", params={"limit": 2}).json()
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    timestamps = [entry["at"] for entry in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True), "default sort must be newest first"
