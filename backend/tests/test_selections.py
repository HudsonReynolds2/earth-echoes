"""Gate 35: E2.5 selection engine (spec 5.2, 13; DECISIONS D54).

The structured-JSON grammar (caps, unknown/secret key checks), predicate
semantics (tags in parity with E1.7's list filter, effective-value
comparisons through inheritance, the at-or-above `exists` definition, the
`ids` checkbox path), evaluation-time visibility re-filtering, and the
saved-selection lifecycle: store the query, re-evaluate at use.
"""

import uuid

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

OWNER = "sel-owner@example.com"
VIEWER = "sel-viewer@example.com"
OP_A = "sel-op-a@example.com"

MACS_A = ["02:E2:05:00:01:01", "02:E2:05:00:01:02", "02:E2:05:00:01:03"]
MAC_B = "02:E2:05:00:02:01"


@pytest.fixture(scope="module")
def sel_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate35-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="sel-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="sel-dep-a", slug="sel-dep-a")
        dep_b = Deployment(organization_id=org.id, name="sel-dep-b", slug="sel-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a = Pod(deployment_id=dep_a.id, name="sel-pod-a", tags=["coastal"])
        pod_b = Pod(deployment_id=dep_b.id, name="sel-pod-b", tags=["ridge"])
        db.add_all([pod_a, pod_b])
        db.flush()
        agg_a = Aggregator(pod_id=pod_a.id, aggregator_uuid="sel-agg-a")
        agg_b = Aggregator(pod_id=pod_b.id, aggregator_uuid="sel-agg-b")
        db.add_all([agg_a, agg_b])
        db.flush()
        for index, mac in enumerate(MACS_A):
            db.add(
                Listener(
                    mac=mac,
                    name=f"sel-lst-a{index + 1}",
                    aggregator_id=agg_a.id,
                    deployment_id=dep_a.id,
                    tags=["coastal"] if index == 0 else [],
                )
            )
        db.add(
            Listener(mac=MAC_B, name="sel-lst-b1", aggregator_id=agg_b.id, deployment_id=dep_b.id)
        )
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
            "dep_a": str(dep_a.id),
            "dep_b": str(dep_b.id),
            "pod_a": str(pod_a.id),
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


def _preview(client, query, **params):
    return client.post(f"{API_PREFIX}/selections/preview", json=query, params=params)


def _macs(response) -> list[str]:
    return [item["entity_id"] for item in response.json()["items"]]


# =========================================================================
# Grammar validation
# =========================================================================


def test_bad_shapes_are_pydantic_422s(sel_app):
    owner = _login(sel_app, OWNER)
    cases = [
        {
            "entity_type": "listener",
            "where": {"key": "logging.verbosity", "op": "regex", "value": "x"},
        },
        {
            "entity_type": "listener",
            "where": {"key": "logging.verbosity", "op": "exists", "value": 1},
        },
        {"entity_type": "fleet"},
        {"entity_type": "listener", "where": {"all": []}},
        {"entity_type": "listener", "extra_field": 1},
    ]
    for query in cases:
        assert _preview(owner, query).status_code == 422, query


def test_semantic_errors_name_their_keys(sel_app):
    owner = _login(sel_app, OWNER)
    unknown = _preview(
        owner,
        {"entity_type": "listener", "where": {"key": "zz.unknown", "op": "eq", "value": 1}},
    )
    assert unknown.status_code == 422
    assert "zz.unknown" in str(unknown.json()["error"]["detail"]["errors"])

    secret_eq = _preview(
        owner,
        {
            "entity_type": "listener",
            "where": {"key": "network.wifi_password", "op": "eq", "value": "x"},
        },
    )
    assert secret_eq.status_code == 422
    assert "secret" in str(secret_eq.json()["error"]["detail"]["errors"])

    # exists on a secret key is fine: set-ness is not a value (D54).
    secret_exists = _preview(
        owner,
        {"entity_type": "listener", "where": {"key": "network.wifi_password", "op": "exists"}},
    )
    assert secret_exists.status_code == 200


def test_depth_cap(sel_app):
    owner = _login(sel_app, OWNER)
    node = {"tag": "coastal"}
    for _ in range(6):
        node = {"all": [node]}
    response = _preview(owner, {"entity_type": "listener", "where": node})
    assert response.status_code == 422
    assert "nesting" in str(response.json()["error"]["detail"]["errors"])


# =========================================================================
# Predicate semantics
# =========================================================================


def test_tag_predicate_matches_e17_containment(sel_app):
    owner = _login(sel_app, OWNER)
    by_selection = _preview(owner, {"entity_type": "listener", "where": {"tag": "coastal"}})
    by_list_filter = owner.get(f"{API_PREFIX}/listeners", params={"tag": "coastal"})
    assert _macs(by_selection) == [row["mac"] for row in by_list_filter.json()["items"]]
    assert _macs(by_selection) == [MACS_A[0]]


def test_effective_value_predicates_ride_inheritance(sel_app):
    owner = _login(sel_app, OWNER)
    ids = sel_app.state.ids
    # Set 96k at pod A: its listeners match eq 96000; everyone else still
    # matches the 48k default - the predicate reads EFFECTIVE values.
    put = owner.put(
        f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides",
        json={"overrides": {"audio.sample_rate_hz": 96000}},
        headers=_csrf(owner),
    )
    assert put.status_code == 200
    fast = _preview(
        owner,
        {
            "entity_type": "listener",
            "where": {"key": "audio.sample_rate_hz", "op": "eq", "value": 96000},
        },
    )
    assert _macs(fast) == MACS_A
    stock = _preview(
        owner,
        {
            "entity_type": "listener",
            "where": {"key": "audio.sample_rate_hz", "op": "eq", "value": 48000},
        },
    )
    assert _macs(stock) == [MAC_B]


def test_exists_means_override_at_or_above(sel_app):
    owner = _login(sel_app, OWNER)
    exists = _preview(
        owner,
        {"entity_type": "listener", "where": {"key": "audio.sample_rate_hz", "op": "exists"}},
    )
    assert _macs(exists) == MACS_A  # pod-level override covers its listeners
    # Inventory keys never "exist" as overrides, by definition.
    inventory = _preview(
        owner, {"entity_type": "listener", "where": {"key": "identity.name", "op": "exists"}}
    )
    assert _macs(inventory) == []


def test_ids_predicate_normalizes_macs(sel_app):
    owner = _login(sel_app, OWNER)
    response = _preview(
        owner,
        {"entity_type": "listener", "where": {"ids": [MACS_A[1].lower(), "02-e2-05-00-02-01"]}},
    )
    assert _macs(response) == [MACS_A[1], MAC_B]


def test_all_any_nesting_and_in(sel_app):
    owner = _login(sel_app, OWNER)
    query = {
        "entity_type": "listener",
        "scope": {"deployment_id": sel_app.state.ids["dep_a"]},
        "where": {
            "any": [
                {"tag": "coastal"},
                {
                    "all": [
                        {"key": "audio.sample_rate_hz", "op": "in", "value": [96000, 192000]},
                        {"ids": [MACS_A[2]]},
                    ]
                },
            ]
        },
    }
    assert _macs(_preview(owner, query)) == [MACS_A[0], MACS_A[2]]


def test_non_listener_entity_types(sel_app):
    owner = _login(sel_app, OWNER)
    pods = _preview(owner, {"entity_type": "pod", "where": {"tag": "ridge"}})
    assert [item["name"] for item in pods.json()["items"]] == ["sel-pod-b"]
    aggregators = _preview(owner, {"entity_type": "aggregator"})
    assert {item["name"] for item in aggregators.json()["items"]} == {"sel-agg-a", "sel-agg-b"}


def test_preview_paginates_deterministically(sel_app):
    owner = _login(sel_app, OWNER)
    first = _preview(owner, {"entity_type": "listener"}, limit=2, offset=0)
    second = _preview(owner, {"entity_type": "listener"}, limit=2, offset=2)
    assert first.json()["total"] == 4
    assert len(first.json()["items"]) == 2
    assert _macs(first) + _macs(second) == sorted([*MACS_A, MAC_B])


# =========================================================================
# Visibility and the saved-selection lifecycle
# =========================================================================


def test_evaluation_refilters_through_visible_deployments(sel_app):
    operator = _login(sel_app, OP_A)
    everything = _preview(operator, {"entity_type": "listener"})
    assert _macs(everything) == MACS_A  # dep_b's listener invisible to a scoped operator
    scoped_elsewhere = _preview(
        operator,
        {"entity_type": "listener", "scope": {"deployment_id": sel_app.state.ids["dep_b"]}},
    )
    assert _macs(scoped_elsewhere) == []


def test_saved_selections_reevaluate_at_use(sel_app):
    owner = _login(sel_app, OWNER)
    created = owner.post(
        f"{API_PREFIX}/selections",
        json={
            "name": "coastal listeners",
            "query": {"entity_type": "listener", "where": {"tag": "coastal"}},
        },
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["query"] == {"entity_type": "listener", "where": {"tag": "coastal"}}

    listed = owner.get(f"{API_PREFIX}/selections")
    assert [row["name"] for row in listed.json()["items"]] == ["coastal listeners"]

    # The fleet grows; the SAME stored query now matches the new device.
    factory = sel_app.state.session_factory
    with factory() as db:
        agg_id = uuid.UUID(sel_app.state.ids["agg_a"])
        dep_id = uuid.UUID(sel_app.state.ids["dep_a"])
        db.add(
            Listener(
                mac="02:E2:05:00:01:99",
                name="sel-lst-new",
                aggregator_id=agg_id,
                deployment_id=dep_id,
                tags=["coastal"],
            )
        )
        db.commit()
    reevaluated = _preview(owner, listed.json()["items"][0]["query"])
    assert _macs(reevaluated) == [MACS_A[0], "02:E2:05:00:01:99"]

    with factory() as db:
        detail = db.scalar(select(AuditLog.detail).where(AuditLog.action == "selection.create"))
    assert detail == {"name": "coastal listeners", "entity_type": "listener"}


def test_create_requires_manage_config_somewhere_and_unique_name(sel_app):
    viewer = _login(sel_app, VIEWER)
    denied = viewer.post(
        f"{API_PREFIX}/selections",
        json={"name": "viewer tries", "query": {"entity_type": "listener"}},
        headers=_csrf(viewer),
    )
    assert denied.status_code == 403

    operator = _login(sel_app, OP_A)  # scoped MANAGE_CONFIG is enough
    ok = operator.post(
        f"{API_PREFIX}/selections",
        json={"name": "op selection", "query": {"entity_type": "listener"}},
        headers=_csrf(operator),
    )
    assert ok.status_code == 201

    duplicate = operator.post(
        f"{API_PREFIX}/selections",
        json={"name": "op selection", "query": {"entity_type": "listener"}},
        headers=_csrf(operator),
    )
    assert duplicate.status_code == 409


def test_unauthenticated_is_401(sel_app):
    anonymous = TestClient(sel_app, raise_server_exceptions=False)
    assert (
        anonymous.post(
            f"{API_PREFIX}/selections/preview", json={"entity_type": "listener"}
        ).status_code
        == 401
    )
