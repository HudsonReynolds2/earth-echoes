"""Gate 25: E1.6 bulk import (spec 13; DECISIONS D38).

Mixed-row results with correct row numbers, the all-or-nothing default with
the audit row provably rolled back, partial accept committing only valid
rows, the explicit auto-suffix parameter, in-file duplicates, CSV/JSON
parity from one fixture, strict CSV headers, size limits, and per-row scope
enforcement for a deployment-scoped operator.
"""

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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

OWNER = "imp-owner@example.com"
OP_A = "imp-op-a@example.com"
VIEWER = "imp-viewer@example.com"


@pytest.fixture(scope="module")
def imp_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate25-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="imp-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="imp-dep-a", slug="imp-dep-a")
        dep_b = Deployment(organization_id=org.id, name="imp-dep-b", slug="imp-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a = Pod(deployment_id=dep_a.id, name="imp-pod-a")
        pod_b = Pod(deployment_id=dep_b.id, name="imp-pod-b")
        pod_bare = Pod(deployment_id=dep_b.id, name="imp-pod-bare")
        db.add_all([pod_a, pod_b, pod_bare])
        db.flush()
        db.add_all(
            [
                Aggregator(pod_id=pod_a.id, aggregator_uuid="imp-agg-a"),
                Aggregator(pod_id=pod_b.id, aggregator_uuid="imp-agg-b"),
            ]
        )
        for email, role, scope in (
            (OWNER, "owner", None),
            (OP_A, "deployment_operator", dep_a.id),
            (VIEWER, "viewer", None),
        ):
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
        app.state.ids = {"pod_b": str(pod_b.id), "pod_bare": str(pod_bare.id)}
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def _import_json(client, rows, entity="listeners", **params):
    return client.post(
        f"{API_PREFIX}/{entity}/import",
        params=params,
        json={"rows": rows},
        headers=_csrf(client),
    )


def _listener_count(app) -> int:
    factory = app.state.session_factory
    with factory() as db:
        return db.scalar(select(func.count()).select_from(Listener)) or 0


def _audit_count(app, action: str) -> int:
    factory = app.state.session_factory
    with factory() as db:
        return (
            db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == action))
            or 0
        )


def test_all_or_nothing_rolls_back_rows_and_audit(imp_app):
    owner = _login(imp_app, OWNER)
    before_rows = _listener_count(imp_app)
    before_audit = _audit_count(imp_app, "listener.import")
    response = _import_json(
        owner,
        [
            {"mac": "02:E1:06:00:00:01", "name": "aon-1", "aggregator_uuid": "imp-agg-a"},
            {"mac": "not-a-mac", "name": "aon-2", "aggregator_uuid": "imp-agg-a"},
            {"mac": "02:E1:06:00:00:03", "name": "aon-3", "aggregator_uuid": "imp-agg-a"},
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["committed"] is False and body["created"] == 0 and body["failed"] == 1
    assert [r["row"] for r in body["rows"]] == [1, 2, 3]
    assert [r["status"] for r in body["rows"]] == ["created", "error", "created"]
    assert body["rows"][1]["error"]["code"] == "validation_error"
    # Nothing landed - not the listeners, not the audit row (D38).
    assert _listener_count(imp_app) == before_rows
    assert _audit_count(imp_app, "listener.import") == before_audit


def test_partial_accept_commits_only_the_valid_rows(imp_app):
    owner = _login(imp_app, OWNER)
    response = _import_json(
        owner,
        [
            {"mac": "02:E1:06:00:00:01", "name": "part-1", "aggregator_uuid": "imp-agg-a"},
            {"mac": "bogus", "name": "part-2", "aggregator_uuid": "imp-agg-a"},
        ],
        partial="true",
    )
    body = response.json()
    assert body["committed"] is True and body["created"] == 1 and body["failed"] == 1
    factory = imp_app.state.session_factory
    with factory() as db:
        assert db.get(Listener, "02:E1:06:00:00:01") is not None
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "listener.import")).first()
    assert audit is not None
    assert audit.detail["partial"] is True and audit.detail["created_ids"] == ["02:E1:06:00:00:01"]


def test_auto_suffix_param_and_in_file_duplicates(imp_app):
    owner = _login(imp_app, OWNER)
    # Without the parameter: colliding name is a row error.
    rejected = _import_json(
        owner,
        [{"mac": "02:E1:06:00:00:04", "name": "part-1", "aggregator_uuid": "imp-agg-a"}],
    )
    assert rejected.json()["rows"][0]["error"]["code"] == "conflict"
    # With it: DB collision suffixes, and an in-file repeat suffixes again,
    # because flushed rows are visible to later rows' checks.
    accepted = _import_json(
        owner,
        [
            {"mac": "02:E1:06:00:00:04", "name": "part-1", "aggregator_uuid": "imp-agg-a"},
            {"mac": "02:E1:06:00:00:05", "name": "part-1", "aggregator_uuid": "imp-agg-a"},
        ],
        auto_suffix="true",
    )
    names = [r["name"] for r in accepted.json()["rows"]]
    assert accepted.json()["committed"] is True
    assert names == ["part-1-2", "part-1-3"]
    # In-file duplicate MAC is a row conflict either way.
    dup_mac = _import_json(
        owner,
        [
            {"mac": "02:E1:06:00:00:06", "name": "dm-1", "aggregator_uuid": "imp-agg-a"},
            {"mac": "02:e1:06:00:00:06", "name": "dm-2", "aggregator_uuid": "imp-agg-a"},
        ],
        partial="true",
    )
    statuses = [r["status"] for r in dup_mac.json()["rows"]]
    assert statuses == ["created", "error"]
    assert dup_mac.json()["rows"][1]["error"]["code"] == "conflict"


def test_csv_and_json_parity_including_gps_and_tags(imp_app):
    owner = _login(imp_app, OWNER)
    csv_text = (
        "mac,name,aggregator_uuid,gps_lat,gps_lon,tags\n"
        "02:E1:06:00:00:07,csv-1,imp-agg-a,47.64,-121.88,coastal|solar\n"
        "02:E1:06:00:00:08,csv-2,imp-agg-a,,,\n"
    )
    csv_response = owner.post(
        f"{API_PREFIX}/listeners/import",
        content=csv_text,
        headers={**_csrf(owner), "Content-Type": "text/csv"},
    )
    assert csv_response.status_code == 200
    csv_body = csv_response.json()
    assert csv_body["committed"] is True and csv_body["created"] == 2

    json_response = _import_json(
        owner,
        [
            {
                "mac": "02:E1:06:00:00:09",
                "name": "json-1",
                "aggregator_uuid": "imp-agg-a",
                "gps_lat": 47.64,
                "gps_lon": -121.88,
                "tags": ["coastal", "solar"],
            },
            {"mac": "02:E1:06:00:00:0A", "name": "json-2", "aggregator_uuid": "imp-agg-a"},
        ],
    )
    json_body = json_response.json()
    assert json_body["committed"] is True and json_body["created"] == 2

    factory = imp_app.state.session_factory
    with factory() as db:
        csv_row = db.get(Listener, "02:E1:06:00:00:07")
        json_row = db.get(Listener, "02:E1:06:00:00:09")
        assert csv_row is not None and json_row is not None
        assert csv_row.tags == json_row.tags == ["coastal", "solar"]
        assert csv_row.gps_lat == json_row.gps_lat == 47.64
        bare = db.get(Listener, "02:E1:06:00:00:08")
        assert bare is not None and bare.gps_lat is None and bare.tags == []


def test_csv_header_and_size_limits(imp_app):
    owner = _login(imp_app, OWNER)
    bad_header = owner.post(
        f"{API_PREFIX}/listeners/import",
        content="mac,name,wrong\n02:E1:06:00:00:0B,x,y\n",
        headers={**_csrf(owner), "Content-Type": "text/csv"},
    )
    assert bad_header.status_code == 422
    assert bad_header.json()["error"]["code"] == "validation_error"

    too_many = _import_json(
        owner,
        [
            {
                "mac": f"02:E1:07:{i // 65536:02X}:{(i // 256) % 256:02X}:{i % 256:02X}",
                "name": f"n{i}",
                "aggregator_uuid": "imp-agg-a",
            }
            for i in range(1001)
        ],
    )
    assert too_many.status_code == 422

    oversized = owner.post(
        f"{API_PREFIX}/listeners/import",
        content="mac,name,aggregator_uuid,gps_lat,gps_lon,tags\n" + ("x" * (1024 * 1024)),
        headers={**_csrf(owner), "Content-Type": "text/csv"},
    )
    assert oversized.status_code == 422


def test_scoped_operator_gets_row_level_forbidden_across_deployments(imp_app):
    op_a = _login(imp_app, OP_A)
    response = _import_json(
        op_a,
        [
            {"mac": "02:E1:06:00:00:0C", "name": "op-ok", "aggregator_uuid": "imp-agg-a"},
            {"mac": "02:E1:06:00:00:0D", "name": "op-no", "aggregator_uuid": "imp-agg-b"},
        ],
        partial="true",
    )
    body = response.json()
    assert [r["status"] for r in body["rows"]] == ["created", "error"]
    assert body["rows"][1]["error"]["code"] == "forbidden"
    assert body["committed"] is True and body["created"] == 1


def test_viewer_rows_all_forbidden_nothing_commits(imp_app):
    viewer = _login(imp_app, VIEWER)
    before = _listener_count(imp_app)
    response = _import_json(
        viewer,
        [{"mac": "02:E1:06:00:00:0E", "name": "v-1", "aggregator_uuid": "imp-agg-a"}],
    )
    body = response.json()
    assert body["committed"] is False and body["failed"] == 1
    assert body["rows"][0]["error"]["code"] == "forbidden"
    assert _listener_count(imp_app) == before


def test_aggregator_import_paths(imp_app):
    owner = _login(imp_app, OWNER)
    factory = imp_app.state.session_factory
    with factory() as db:
        before_aggs = db.scalar(select(func.count()).select_from(Aggregator)) or 0

    # All-or-nothing: occupied pod fails the batch; bare pod row rolls back.
    blocked = _import_json(
        owner,
        [
            {"pod_id": imp_app.state.ids["pod_bare"], "aggregator_uuid": "imp-agg-new"},
            {"pod_id": imp_app.state.ids["pod_b"], "aggregator_uuid": "imp-agg-squat"},
        ],
        entity="aggregators",
    )
    body = blocked.json()
    assert body["committed"] is False
    assert [r["status"] for r in body["rows"]] == ["created", "error"]
    assert body["rows"][1]["error"]["code"] == "conflict"
    with factory() as db:
        assert (db.scalar(select(func.count()).select_from(Aggregator)) or 0) == before_aggs

    # Partial: the bare-pod row lands, generated uuid when omitted; unknown
    # pod is a validation error.
    accepted = _import_json(
        owner,
        [
            {"pod_id": imp_app.state.ids["pod_bare"]},
            {"pod_id": "00000000-0000-0000-0000-000000000000"},
        ],
        entity="aggregators",
        partial="true",
    )
    body = accepted.json()
    assert body["committed"] is True and body["created"] == 1 and body["failed"] == 1
    assert len(body["rows"][0]["name"]) == 32  # platform-assigned uuid4().hex
    assert body["rows"][1]["error"]["code"] == "validation_error"
    assert _audit_count(imp_app, "aggregator.import") == 1
