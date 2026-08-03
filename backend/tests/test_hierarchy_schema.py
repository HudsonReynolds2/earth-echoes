"""Gate 20: E1.1 hierarchy schema constraints (spec 4.1-4.3; DECISIONS D30-D33).

Every rule the phase document fixes as a DATABASE constraint is proven here by
raw inserts expecting IntegrityError, not by application-layer checks: MAC
uniqueness and format, one aggregator per pod, listener names unique within a
deployment (and free across deployments), pod/deployment names unique within
their parent, slug format and uniqueness, aggregator_uuid uniqueness, the
role_assignment FK closing E0.7's seam, and audit_log.scope staying FK-free.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from test_auth import pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.db import create_session_factory
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

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def factory(pg_url):  # noqa: F811
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def scaffold(factory):
    """One org, two deployments, pods and aggregators to hang listeners on."""
    with factory() as db:
        org = Organization(name="schema-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="dep-a", slug="schema-dep-a")
        dep_b = Deployment(organization_id=org.id, name="dep-b", slug="schema-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_a1 = Pod(deployment_id=dep_a.id, name="pod-a1")
        pod_a2 = Pod(deployment_id=dep_a.id, name="pod-a2")
        pod_b1 = Pod(deployment_id=dep_b.id, name="pod-b1")
        db.add_all([pod_a1, pod_a2, pod_b1])
        db.flush()
        agg_a1 = Aggregator(pod_id=pod_a1.id, aggregator_uuid="schema-agg-a1")
        agg_a2 = Aggregator(pod_id=pod_a2.id, aggregator_uuid="schema-agg-a2")
        agg_b1 = Aggregator(pod_id=pod_b1.id, aggregator_uuid="schema-agg-b1")
        db.add_all([agg_a1, agg_a2, agg_b1])
        db.commit()
        return {
            "org": org.id,
            "dep_a": dep_a.id,
            "dep_b": dep_b.id,
            "pod_a1": pod_a1.id,
            "agg_a1": agg_a1.id,
            "agg_a2": agg_a2.id,
            "agg_b1": agg_b1.id,
        }


def _listener(scaffold, mac, name, agg="agg_a1", dep="dep_a"):
    return Listener(mac=mac, name=name, aggregator_id=scaffold[agg], deployment_id=scaffold[dep])


def _expect_integrity_error(factory, row):
    with factory() as db:
        db.add(row)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_duplicate_mac_insert_fails(factory, scaffold):
    with factory() as db:
        db.add(_listener(scaffold, "AA:BB:CC:DD:EE:01", "mac-holder"))
        db.commit()
    # Same MAC under a different aggregator AND a different deployment: the
    # primary key rejects it globally (spec 4.2: MAC is platform-wide identity).
    _expect_integrity_error(
        factory, _listener(scaffold, "AA:BB:CC:DD:EE:01", "mac-thief", agg="agg_b1", dep="dep_b")
    )


def test_malformed_mac_fails_the_check_constraint(factory, scaffold):
    for bad in ("aa:bb:cc:dd:ee:02", "AA-BB-CC-DD-EE-02", "AA:BB:CC:DD:EE", "AABBCCDDEE02"):
        _expect_integrity_error(factory, _listener(scaffold, bad, f"bad-{bad}"))


def test_second_aggregator_on_a_pod_fails(factory, scaffold):
    _expect_integrity_error(
        factory, Aggregator(pod_id=scaffold["pod_a1"], aggregator_uuid="schema-agg-squatter")
    )


def test_duplicate_listener_name_within_deployment_fails_across_pods(factory, scaffold):
    with factory() as db:
        db.add(_listener(scaffold, "AA:BB:CC:DD:EE:03", "shared-name", agg="agg_a1"))
        db.commit()
    # Different pod, different aggregator, SAME deployment: still rejected.
    _expect_integrity_error(
        factory, _listener(scaffold, "AA:BB:CC:DD:EE:04", "shared-name", agg="agg_a2")
    )


def test_same_listener_name_across_deployments_succeeds(factory, scaffold):
    with factory() as db:
        db.add(_listener(scaffold, "AA:BB:CC:DD:EE:05", "cross-dep-name", agg="agg_a1"))
        db.add(
            _listener(scaffold, "AA:BB:CC:DD:EE:06", "cross-dep-name", agg="agg_b1", dep="dep_b")
        )
        db.commit()


def test_duplicate_pod_name_within_deployment_fails(factory, scaffold):
    _expect_integrity_error(factory, Pod(deployment_id=scaffold["dep_a"], name="pod-a1"))
    # ... while the same pod name under the other deployment is fine.
    with factory() as db:
        db.add(Pod(deployment_id=scaffold["dep_b"], name="pod-a1"))
        db.commit()


def test_duplicate_deployment_name_within_org_fails(factory, scaffold):
    _expect_integrity_error(
        factory, Deployment(organization_id=scaffold["org"], name="dep-a", slug="another-slug")
    )


def test_duplicate_slug_fails_even_across_names(factory, scaffold):
    _expect_integrity_error(
        factory,
        Deployment(organization_id=scaffold["org"], name="fresh-name", slug="schema-dep-a"),
    )


def test_malformed_slug_fails_the_check_constraint(factory, scaffold):
    for bad in ("Has-Upper", "ends-", "-starts", "spaced out", "under_score"):
        _expect_integrity_error(
            factory,
            Deployment(organization_id=scaffold["org"], name=f"slug-{bad}", slug=bad),
        )


def test_duplicate_aggregator_uuid_fails(factory, scaffold):
    with factory() as db:
        pod = Pod(deployment_id=scaffold["dep_b"], name="pod-b2")
        db.add(pod)
        db.commit()
        pod_id = pod.id
    _expect_integrity_error(factory, Aggregator(pod_id=pod_id, aggregator_uuid="schema-agg-a1"))


def test_orphan_scoped_grant_now_fails_the_foreign_key(factory):
    """The E0.7 seam is closed (D33): a scoped grant must reference a real
    deployment row."""
    with factory() as db:
        user = User(email="schema-orphan@example.com", password_hash=hash_password("x" * 12))
        db.add(user)
        db.commit()
        user_id = user.id
    _expect_integrity_error(
        factory,
        RoleAssignment(user_id=user_id, role="field_tech", deployment_id=uuid.uuid4()),
    )


def test_audit_scope_still_accepts_a_dangling_uuid(factory):
    """audit_log.scope is deliberately NOT a foreign key (D3/D33): audit rows
    must be writable with - and outlive - any deployment id."""
    with factory() as db:
        db.add(
            AuditLog(
                action="schema.probe",
                entity_type="deployment",
                entity_id="schema-probe",
                actor_user_id=None,
                scope=uuid.uuid4(),
            )
        )
        db.commit()
