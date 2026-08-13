"""Gate 34: E2.4 effective and override endpoints (spec 13; D35, D50-D51).

The wire surface over E2.2/E2.3: per-key provenance on effective reads,
wholesale-replace PUT folding all validation errors into one 422, the
secret sentinel round-trip over HTTP, the D35 scope matrix carried over
verbatim, audit rows holding key names only, and override cleanup wired
into the E1 DELETE endpoints.
"""

import uuid

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.config.catalog import CATALOG_VERSION
from app.config.overrides import secret_name
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    Deployment,
    EntityOverride,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "cfg-owner@example.com"
VIEWER = "cfg-viewer@example.com"
OP_A = "cfg-op-a@example.com"

MAC_A = "02:E2:04:00:00:01"
MAC_B = "02:E2:04:00:00:02"


@pytest.fixture(scope="module")
def cfg_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate34-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="cfg-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="cfg-dep-a", slug="cfg-dep-a")
        dep_b = Deployment(organization_id=org.id, name="cfg-dep-b", slug="cfg-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a = Pod(deployment_id=dep_a.id, name="cfg-pod-a")
        pod_b = Pod(deployment_id=dep_b.id, name="cfg-pod-b")
        db.add_all([pod_a, pod_b])
        db.flush()
        agg_a = Aggregator(pod_id=pod_a.id, aggregator_uuid="cfg-agg-a")
        agg_b = Aggregator(pod_id=pod_b.id, aggregator_uuid="cfg-agg-b")
        db.add_all([agg_a, agg_b])
        db.flush()
        db.add_all(
            [
                Listener(
                    mac=MAC_A, name="cfg-lst-a", aggregator_id=agg_a.id, deployment_id=dep_a.id
                ),
                Listener(
                    mac=MAC_B, name="cfg-lst-b", aggregator_id=agg_b.id, deployment_id=dep_b.id
                ),
            ]
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
            "org": str(org.id),
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


def _put(client, path, overrides):
    return client.put(path, json={"overrides": overrides}, headers=_csrf(client))


# =========================================================================
# Wire shapes
# =========================================================================


def test_effective_carries_per_key_provenance(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    assert (
        _put(
            owner,
            f"{API_PREFIX}/organizations/{ids['org']}/config/overrides",
            {"logging.verbosity": "warn"},
        ).status_code
        == 200
    )
    assert (
        _put(
            owner,
            f"{API_PREFIX}/deployments/{ids['dep_a']}/config/overrides",
            {"audio.sample_rate_hz": 96000},
        ).status_code
        == 200
    )
    assert (
        _put(
            owner,
            f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides",
            {"network.wifi_ssid": "cfg-field"},
        ).status_code
        == 200
    )

    body = owner.get(f"{API_PREFIX}/listeners/{MAC_A}/config/effective").json()
    assert body["entity_type"] == "listener"
    assert body["entity_id"] == MAC_A
    assert body["catalog_version"] == CATALOG_VERSION
    config = body["config"]
    assert len(config) == 38  # every catalog key, inventory included at listener
    assert config["logging.verbosity"] == {
        "value": "warn",
        "source": "organization",
        "source_entity_id": ids["org"],
    }
    assert config["audio.sample_rate_hz"]["source"] == "deployment"
    assert config["network.wifi_ssid"]["source"] == "pod"
    assert config["identity.name"] == {
        "value": "cfg-lst-a",
        "source": "inventory",
        "source_entity_id": MAC_A,
    }
    assert config["capture.mode"] == {
        "value": "duty_cycle",
        "source": "default",
        "source_entity_id": None,
    }


def test_put_is_wholesale_replace_and_audits_key_names(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    path = f"{API_PREFIX}/aggregators/{ids['agg_a']}/config/overrides"
    first = _put(owner, path, {"analysis.model_id": "birdnet-v2", "upload.s3_prefix": "pod-a/"})
    assert first.status_code == 200
    assert first.json()["overrides"] == {
        "analysis.model_id": "birdnet-v2",
        "upload.s3_prefix": "pod-a/",
    }
    replaced = _put(owner, path, {"analysis.model_id": "birdnet-v3"})
    assert replaced.json()["overrides"] == {"analysis.model_id": "birdnet-v3"}

    factory = cfg_app.state.session_factory
    with factory() as db:
        details = [
            row
            for row in db.scalars(
                select(AuditLog.detail)
                .where(
                    AuditLog.action == "config.override_update",
                    AuditLog.entity_id == ids["agg_a"],
                )
                .order_by(AuditLog.at)
            ).all()
        ]
    assert details[-1] == {
        "set": ["analysis.model_id"],
        "unset": ["upload.s3_prefix"],
        "catalog_version": CATALOG_VERSION,
    }
    for detail in details:  # key NAMES only, never values
        assert "birdnet" not in str(detail.values())


def test_validation_errors_fold_into_one_422(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    response = _put(
        owner,
        f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides",
        {
            "zz.unknown": 1,
            "telemetry.influx_url": "https://x",
            "audio.sample_rate_hz": 44100,
        },
    )
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    errors = body["detail"]["errors"]
    assert [e["key"] for e in errors] == [
        "audio.sample_rate_hz",
        "telemetry.influx_url",
        "zz.unknown",
    ]
    assert {e["code"] for e in errors} == {"invalid_value", "service_restricted", "unknown_key"}
    # And nothing was staged by the failed PUT.
    current = owner.get(f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides").json()
    assert "audio.sample_rate_hz" not in current["overrides"]


def test_extra_body_fields_rejected(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    response = owner.put(
        f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides",
        json={"overrides": {}, "level": "pod"},
        headers=_csrf(owner),
    )
    assert response.status_code == 422


# =========================================================================
# Secrets over the wire
# =========================================================================


def test_secret_sentinel_round_trip_over_http(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    path = f"{API_PREFIX}/pods/{ids['pod_b']}/config/overrides"
    plaintext = f"wire-psk-{uuid.uuid4().hex}"

    set_response = _put(owner, path, {"network.wifi_password": plaintext})
    assert set_response.status_code == 200
    assert set_response.json()["overrides"]["network.wifi_password"] == {"$secret_set": True}
    assert plaintext not in set_response.text

    read = owner.get(path).json()
    assert read["overrides"]["network.wifi_password"] == {"$secret_set": True}

    effective = owner.get(f"{API_PREFIX}/listeners/{MAC_B}/config/effective")
    assert effective.json()["config"]["network.wifi_password"]["value"] == {"$secret_set": True}
    assert plaintext not in effective.text

    # The sentinel round-trips a redacted GET back through PUT: keep, not clear.
    keep = _put(
        owner, path, {"network.wifi_password": {"$secret_set": True}, "network.wifi_ssid": "b-net"}
    )
    assert keep.status_code == 200
    store = cfg_app.state.secret_store
    name = secret_name("pod", ids["pod_b"], "network.wifi_password")
    assert store.get(name) == plaintext  # kept, not rotated

    # Omission unsets; the stored secret is deleted after the commit.
    unset = _put(owner, path, {"network.wifi_ssid": "b-net"})
    assert unset.status_code == 200
    assert not store.exists(name)


def test_keep_sentinel_with_nothing_stored_is_422(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    response = _put(
        owner,
        f"{API_PREFIX}/deployments/{ids['dep_b']}/config/overrides",
        {"network.wifi_password": {"$secret_set": True}},
    )
    assert response.status_code == 422
    (error,) = response.json()["error"]["detail"]["errors"]
    assert "no stored secret to keep" in error["message"]


# =========================================================================
# Scope discipline (D35 carried over)
# =========================================================================


def test_org_write_needs_an_org_wide_grant(cfg_app):
    ids = cfg_app.state.ids
    path = f"{API_PREFIX}/organizations/{ids['org']}/config/overrides"
    operator = _login(cfg_app, OP_A)  # scoped MANAGE_CONFIG only
    denied = _put(operator, path, {"logging.verbosity": "debug"})
    assert denied.status_code == 403
    assert "organization-wide" in denied.json()["error"]["message"]
    viewer = _login(cfg_app, VIEWER)
    assert _put(viewer, path, {"logging.verbosity": "debug"}).status_code == 403
    assert viewer.get(path).status_code == 200  # reads are every-role


def test_deployment_403_before_lookup(cfg_app):
    operator = _login(cfg_app, OP_A)
    ids = cfg_app.state.ids
    # Out-of-scope deployment: 403 (ids are not enumerable).
    assert (
        operator.get(f"{API_PREFIX}/deployments/{ids['dep_b']}/config/effective").status_code == 403
    )
    # Nonexistent deployment, no permission there either: STILL 403, before
    # any lookup could reveal nonexistence.
    assert (
        operator.get(f"{API_PREFIX}/deployments/{uuid.uuid4()}/config/effective").status_code == 403
    )
    # In scope: writes work.
    ok = _put(
        operator,
        f"{API_PREFIX}/deployments/{ids['dep_a']}/config/overrides",
        {"listener.wake_grace_seconds": 45},
    )
    assert ok.status_code == 200


def test_child_items_answer_identical_404(cfg_app):
    operator = _login(cfg_app, OP_A)
    ids = cfg_app.state.ids
    missing_mac = "02:00:00:00:00:99"
    out_of_scope = operator.get(f"{API_PREFIX}/listeners/{MAC_B}/config/effective")
    missing = operator.get(f"{API_PREFIX}/listeners/{missing_mac}/config/effective")
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.json()["error"] == missing.json()["error"]  # byte-identical, no oracle
    assert (
        _put(
            operator,
            f"{API_PREFIX}/pods/{ids['pod_b']}/config/overrides",
            {"network.wifi_ssid": "x"},
        ).status_code
        == 404
    )
    assert (
        operator.get(f"{API_PREFIX}/aggregators/{ids['agg_b']}/config/overrides").status_code == 404
    )


def test_viewer_reads_but_never_writes(cfg_app):
    viewer = _login(cfg_app, VIEWER)
    ids = cfg_app.state.ids
    assert viewer.get(f"{API_PREFIX}/pods/{ids['pod_a']}/config/effective").status_code == 200
    denied = _put(viewer, f"{API_PREFIX}/pods/{ids['pod_a']}/config/overrides", {})
    assert (
        denied.status_code == 404
    )  # viewer lacks MANAGE_CONFIG anywhere -> resolve fails D35-style


def test_unauthenticated_is_401(cfg_app):
    anonymous = TestClient(cfg_app, raise_server_exceptions=False)
    ids = cfg_app.state.ids
    assert anonymous.get(f"{API_PREFIX}/pods/{ids['pod_a']}/config/effective").status_code == 401


# =========================================================================
# DELETE cleanup (wired into the E1 endpoints)
# =========================================================================


def test_entity_delete_removes_overrides_and_secrets_post_commit(cfg_app):
    owner = _login(cfg_app, OWNER)
    ids = cfg_app.state.ids
    factory = cfg_app.state.session_factory
    store = cfg_app.state.secret_store

    # A bare pod (no aggregator) holding a secret override.
    create = owner.post(
        f"{API_PREFIX}/pods",
        json={"deployment_id": ids["dep_a"], "name": "cfg-doomed-pod"},
        headers=_csrf(owner),
    )
    assert create.status_code == 201
    pod_id = create.json()["id"]
    plaintext = f"doomed-psk-{uuid.uuid4().hex}"
    assert (
        _put(
            owner,
            f"{API_PREFIX}/pods/{pod_id}/config/overrides",
            {"network.wifi_password": plaintext, "network.wifi_ssid": "doomed"},
        ).status_code
        == 200
    )
    name = secret_name("pod", pod_id, "network.wifi_password")
    assert store.exists(name)

    assert owner.delete(f"{API_PREFIX}/pods/{pod_id}", headers=_csrf(owner)).status_code == 204
    with factory() as db:
        assert db.scalar(select(EntityOverride).where(EntityOverride.entity_id == pod_id)) is None
    assert not store.exists(name)
