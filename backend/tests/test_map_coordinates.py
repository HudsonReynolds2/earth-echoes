"""E6.2a: coordinates on Deployments and Aggregators (spec 9.2; phase-6 fixed choice 2; D189).

Three layers hold one rule, "both or neither, and in range":

- the API refuses half a pair with a 422 that says how to fix it, judging a
  PATCH by its RESULT (moving one axis of an existing pair is fine);
- the aggregator import refuses it per row, and still accepts every
  aggregator CSV written before E6;
- the database CHECKs refuse it for any path around the API.
"""

import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import Aggregator, AuditLog, Deployment, Organization, Pod, RoleAssignment, User
from app.settings import Settings

pytestmark = [pytest.mark.integration]

OWNER = "coords-owner@example.com"
HALF = "give both latitude and longitude, or neither"


@pytest.fixture(scope="module")
def app():
    with ephemeral_postgres() as url:
        application = create_app(
            Settings(
                database_url=url,
                session_secret="e6-coords",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
            )
        )
        with application.state.session_factory() as db:
            org = Organization(name="Coords Org")
            db.add(org)
            owner = User(email=OWNER, password_hash=hash_password(PASSWORD))
            owner.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
            db.add(owner)
            db.commit()
            application.state.org_id = str(org.id)
        yield application


@pytest.fixture(scope="module")
def owner(app) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def make_deployment(app, owner, **coords) -> dict:
    response = owner.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": app.state.org_id, "name": f"dep-{uuid.uuid4().hex[:8]}", **coords},
        headers=csrf(owner),
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_pod(owner, deployment_id: str, **aggregator) -> dict:
    response = owner.post(
        f"{API_PREFIX}/pods",
        json={
            "deployment_id": deployment_id,
            "name": f"pod-{uuid.uuid4().hex[:8]}",
            "aggregator": aggregator,
        },
        headers=csrf(owner),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Deployments --------------------------------------------------------------


def test_a_deployment_is_created_placed_or_unplaced(app, owner):
    placed = make_deployment(app, owner, gps_lat=41.213, gps_lon=-124.004)
    assert (placed["gps_lat"], placed["gps_lon"]) == (41.213, -124.004)
    unplaced = make_deployment(app, owner)
    assert (unplaced["gps_lat"], unplaced["gps_lon"]) == (None, None)


@pytest.mark.parametrize(
    "coords",
    [{"gps_lat": 41.2}, {"gps_lon": -124.0}, {"gps_lat": 41.2, "gps_lon": None}],
)
def test_half_a_pair_is_refused_at_create_with_the_fix_named(app, owner, coords):
    response = owner.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": app.state.org_id, "name": "half", **coords},
        headers=csrf(owner),
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"] == HALF


@pytest.mark.parametrize("coords", [{"gps_lat": 95, "gps_lon": 0}, {"gps_lat": 0, "gps_lon": 181}])
def test_out_of_range_is_refused(app, owner, coords):
    response = owner.post(
        f"{API_PREFIX}/deployments",
        json={"organization_id": app.state.org_id, "name": "far", **coords},
        headers=csrf(owner),
    )
    assert response.status_code == 422


def test_a_patch_is_judged_by_its_result(app, owner):
    placed = make_deployment(app, owner, gps_lat=41.0, gps_lon=-124.0)
    url = f"{API_PREFIX}/deployments/{placed['id']}"

    # Moving one axis of an existing pair leaves a whole pair: allowed.
    moved = owner.patch(url, json={"gps_lat": 41.5}, headers=csrf(owner))
    assert moved.status_code == 200, moved.text
    assert (moved.json()["gps_lat"], moved.json()["gps_lon"]) == (41.5, -124.0)

    # Nulling one axis would leave half: refused, and nothing changes.
    half = owner.patch(url, json={"gps_lon": None}, headers=csrf(owner))
    assert half.status_code == 422 and half.json()["error"]["message"] == HALF
    assert owner.get(url).json()["gps_lon"] == -124.0

    # Clearing both unplaces it.
    cleared = owner.patch(url, json={"gps_lat": None, "gps_lon": None}, headers=csrf(owner))
    assert (cleared.json()["gps_lat"], cleared.json()["gps_lon"]) == (None, None)

    # Setting one axis on an unplaced Deployment is half.
    one = owner.patch(url, json={"gps_lat": 41.0}, headers=csrf(owner))
    assert one.status_code == 422


def test_a_coordinate_edit_is_audited(app, owner):
    placed = make_deployment(app, owner)
    owner.patch(
        f"{API_PREFIX}/deployments/{placed['id']}",
        json={"gps_lat": 43.358, "gps_lon": -121.056},
        headers=csrf(owner),
    )
    with app.state.session_factory() as db:
        detail = db.scalars(
            select(AuditLog.detail).where(
                AuditLog.action == "deployment.update", AuditLog.entity_id == placed["id"]
            )
        ).one()
    assert detail == {"changed": ["gps_lat", "gps_lon"]}


# --- Aggregators (the Pod marker) ---------------------------------------------


def test_a_pod_created_with_its_aggregator_carries_the_position(app, owner):
    deployment = make_deployment(app, owner)
    pod = make_pod(owner, deployment["id"], gps_lat=41.221, gps_lon=-124.012)
    assert (pod["aggregator"]["gps_lat"], pod["aggregator"]["gps_lon"]) == (41.221, -124.012)


def test_half_a_pair_on_the_inline_aggregator_creates_no_pod(app, owner):
    deployment = make_deployment(app, owner)
    response = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": deployment["id"], "name": "half-pod", "aggregator": {"gps_lat": 1}},
        headers=csrf(owner),
    )
    assert response.status_code == 422 and response.json()["error"]["message"] == HALF
    pods = owner.get(f"{API_PREFIX}/pods", params={"deployment_id": deployment["id"]}).json()
    assert pods["total"] == 0


def test_an_aggregator_patch_moves_and_clears_its_position(app, owner):
    deployment = make_deployment(app, owner)
    aggregator_id = make_pod(owner, deployment["id"])["aggregator"]["id"]
    url = f"{API_PREFIX}/aggregators/{aggregator_id}"
    placed = owner.patch(url, json={"gps_lat": 43.367, "gps_lon": -121.066}, headers=csrf(owner))
    assert placed.status_code == 200, placed.text
    assert (placed.json()["gps_lat"], placed.json()["gps_lon"]) == (43.367, -121.066)
    assert owner.patch(url, json={"gps_lat": None}, headers=csrf(owner)).status_code == 422
    cleared = owner.patch(url, json={"gps_lat": None, "gps_lon": None}, headers=csrf(owner))
    assert cleared.json()["gps_lat"] is None


# --- Bulk import ----------------------------------------------------------------


def _import_csv(owner, text: str):
    return owner.post(
        f"{API_PREFIX}/aggregators/import",
        content=text.encode(),
        headers={**csrf(owner), "Content-Type": "text/csv"},
    )


def _empty_pod(app, owner) -> str:
    deployment = make_deployment(app, owner)
    response = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": deployment["id"], "name": f"bare-{uuid.uuid4().hex[:6]}"},
        headers=csrf(owner),
    )
    return response.json()["id"]


def test_a_pre_e6_aggregator_csv_still_imports(app, owner):
    pod_id = _empty_pod(app, owner)
    report = _import_csv(owner, f"pod_id,aggregator_uuid,balena_uuid,name,tags\n{pod_id},,,old,\n")
    assert report.status_code == 200, report.text
    assert report.json()["created"] == 1


def test_an_aggregator_csv_may_carry_coordinates(app, owner):
    pod_id = _empty_pod(app, owner)
    report = _import_csv(
        owner,
        "pod_id,aggregator_uuid,balena_uuid,name,tags,gps_lat,gps_lon\n"
        f"{pod_id},,,with-gps,,41.199,-124.01\n",
    )
    assert report.json()["created"] == 1, report.text
    aggregator_id = report.json()["rows"][0]["entity_id"]
    row = owner.get(f"{API_PREFIX}/aggregators/{aggregator_id}").json()
    assert (row["gps_lat"], row["gps_lon"]) == (41.199, -124.01)


def test_an_import_row_with_half_a_pair_is_a_row_error_naming_the_fix(app, owner):
    pod_id = _empty_pod(app, owner)
    report = _import_csv(
        owner,
        f"pod_id,aggregator_uuid,balena_uuid,name,tags,gps_lat,gps_lon\n{pod_id},,,half,,41.1,\n",
    )
    body = report.json()
    assert body["committed"] is False and body["failed"] == 1
    assert body["rows"][0]["error"] == {"code": "validation_error", "message": HALF}


def test_a_header_with_only_one_coordinate_column_is_malformed(app, owner):
    report = _import_csv(owner, "pod_id,aggregator_uuid,balena_uuid,name,tags,gps_lat\n")
    assert report.status_code == 422
    assert "gps_lat,gps_lon" in report.json()["error"]["message"]


# --- The database is the last line ----------------------------------------------


@pytest.mark.parametrize("model", [Deployment, Aggregator])
@pytest.mark.parametrize(("lat", "lon"), [(41.0, None), (None, -124.0), (95.0, 0.0), (0.0, -181.0)])
def test_the_database_refuses_what_the_api_would(app, model, lat, lon):
    # Its own rows, so the test does not depend on what ran before it.
    with app.state.session_factory() as db:
        deployment = Deployment(
            organization_id=uuid.UUID(app.state.org_id),
            name=f"db-{uuid.uuid4().hex[:8]}",
            slug=f"db-{uuid.uuid4().hex[:8]}",
        )
        db.add(deployment)
        db.flush()
        pod = Pod(deployment_id=deployment.id, name="db-pod")
        db.add(pod)
        db.flush()
        aggregator = Aggregator(pod_id=pod.id, aggregator_uuid=uuid.uuid4().hex)
        db.add(aggregator)
        db.commit()
        row = deployment if model is Deployment else aggregator
        row.gps_lat, row.gps_lon = lat, lon
        with pytest.raises(IntegrityError, match="gps_"):
            db.flush()
        db.rollback()
