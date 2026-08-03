"""Gate 23: E1.4 uniqueness validation and auto-suffix (spec 4.3 item 1).

The reject path with the {field, suggestion} detail the UI dialog consumes,
the explicit-parameter suffix ladder, proof the suffix is never silent, the
MAC always-rejects rule, and the retry-once concurrency path with a suffix
computed against a row that lands mid-flight.
"""

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

import app.api.listeners as listeners_module
from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import AuditLog, Organization, RoleAssignment, User
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "uniq-owner@example.com"


@pytest.fixture(scope="module")
def uniq_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate23-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="uniq-org")
        db.add(org)
        db.flush()
        app.state.org_id = str(org.id)
        user = User(email=OWNER, password_hash=hash_password(PASSWORD))
        user.role_assignments.append(RoleAssignment(role="owner", deployment_id=None))
        db.add(user)
        db.commit()
    return app


@pytest.fixture(scope="module")
def owner(uniq_app):
    client = TestClient(uniq_app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": OWNER, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


@pytest.fixture(scope="module")
def aggregators(uniq_app, owner):
    """Two deployments, each with one pod + aggregator."""
    out = {}
    for marker in ("a", "b"):
        dep = owner.post(
            f"{API_PREFIX}/deployments",
            json={"organization_id": uniq_app.state.org_id, "name": f"Uniq Dep {marker.upper()}"},
            headers=_csrf(owner),
        ).json()["id"]
        pod = owner.post(
            f"{API_PREFIX}/pods",
            json={
                "deployment_id": dep,
                "name": f"uniq-pod-{marker}",
                "aggregator": {"aggregator_uuid": f"uniq-agg-{marker}"},
            },
            headers=_csrf(owner),
        ).json()
        out[marker] = pod["aggregator"]["id"]
    return out


def _create(owner, agg_id, mac, name, **extra):
    return owner.post(
        f"{API_PREFIX}/listeners",
        json={"mac": mac, "name": name, "aggregator_id": agg_id, **extra},
        headers=_csrf(owner),
    )


def test_name_collision_rejects_by_default_with_a_suggestion(owner, aggregators):
    assert _create(owner, aggregators["a"], "02:E1:04:00:00:01", "sensor").status_code == 201
    rejected = _create(owner, aggregators["a"], "02:E1:04:00:00:02", "sensor")
    assert rejected.status_code == 409
    error = rejected.json()["error"]
    assert error["code"] == "conflict"
    # The wire shape the UI's conflict dialog consumes (E1 plan contract).
    assert error["detail"] == {"field": "name", "suggestion": "sensor-2"}


def test_auto_suffix_is_explicit_and_walks_the_ladder(uniq_app, owner, aggregators):
    suffixed = _create(owner, aggregators["a"], "02:E1:04:00:00:03", "sensor", auto_suffix=True)
    assert suffixed.status_code == 201
    assert suffixed.json()["name"] == "sensor-2"
    again = _create(owner, aggregators["a"], "02:E1:04:00:00:04", "sensor", auto_suffix=True)
    assert again.status_code == 201
    assert again.json()["name"] == "sensor-3"

    # The audit row records both names - never a silent rename.
    factory = uniq_app.state.session_factory
    with factory() as db:
        detail = db.scalar(
            select(AuditLog.detail).where(AuditLog.detail["final_name"].astext == "sensor-2")
        )
    assert detail is not None
    assert detail["auto_suffixed"] is True and detail["requested_name"] == "sensor"


def test_suffix_never_applies_without_the_parameter(owner, aggregators):
    """auto_suffix defaults to False: omitting it must reject, not rename."""
    rejected = _create(owner, aggregators["a"], "02:E1:04:00:00:05", "sensor")
    assert rejected.status_code == 409


def test_mac_collision_always_rejects_even_with_auto_suffix(owner, aggregators):
    cloned = _create(owner, aggregators["b"], "02:E1:04:00:00:01", "fresh-name", auto_suffix=True)
    assert cloned.status_code == 409
    assert "already registered" in cloned.json()["error"]["message"]


def test_same_name_is_free_in_the_other_deployment(owner, aggregators):
    other = _create(owner, aggregators["b"], "02:E1:04:00:00:06", "sensor")
    assert other.status_code == 201


def test_suffix_race_retries_once_with_a_recomputed_name(owner, aggregators, monkeypatch):
    """Simulate the compute/flush race: the first suffix the endpoint picks
    is already taken by the time it flushes; the retry recomputes and lands."""
    real = listeners_module.next_free_name
    calls = {"n": 0}

    def racy(db, deployment_id, base):
        calls["n"] += 1
        if calls["n"] == 1:
            return "sensor"  # stale answer: taken since 'the other writer' won
        return real(db, deployment_id, base)

    monkeypatch.setattr(listeners_module, "next_free_name", racy)
    created = _create(owner, aggregators["a"], "02:E1:04:00:00:07", "sensor", auto_suffix=True)
    assert created.status_code == 201
    assert created.json()["name"] == "sensor-4"  # recomputed on the retry
    assert calls["n"] >= 2
