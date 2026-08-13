"""E5.11: rotation and regeneration.

The phase document's acceptance, claim by claim:

* rotation produces **one new revision per Aggregator and zero for Listeners**
  — a property of the projection (spec 5.4 keeps these keys off Listener-bound
  config), asserted here because rotation is the second caller of that path;
* **the old credentials are absent from SecretStore afterwards** — regeneration
  overwrites the same deterministic names rather than accumulating;
* the deployment passes through `pending_verification` and reaches `verified`
  **only after a real test pass**, never optimistically;
* **a rotation whose re-verification fails leaves `services_status` at
  `degraded` and still publishes.** That is the claim worth the most care: the
  intuitive order is wrong, and a later reader "fixing" it would lock every
  device out of the deployment exactly when the credentials had changed under
  them.

The services here are deliberately unreachable — nothing is running at the
generated hostname — so every rotation in this file re-verifies as a FAILURE.
That is the interesting case and it is the default one: it is what an operator
sees in the window between rotating and restarting their stack. The happy path,
where the re-verification passes against a real running stack, is the keystone's
(`test_stack_keystone.py`).
"""

import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    ConfigRevision,
    Deployment,
    EntityOverride,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    Secret,
    User,
)
from app.services.stackgen import STACK_SECRET_ITEMS, stack_secret_name
from app.services.store import load_services
from app.settings import Settings

PASSWORD = "e511-rotate-pw"
ROLE_EMAILS = {
    Role.OWNER: "e511-owner@example.com",
    Role.DEPLOYMENT_OPERATOR: "e511-operator@example.com",
    Role.FIELD_TECH: "e511-tech@example.com",
    Role.VIEWER: "e511-viewer@example.com",
}

#: Two Aggregators and two Listeners, so "one revision per Aggregator and zero
#: per Listener" is a claim with a number in it rather than a coincidence that
#: would also hold at one and zero.
AGGREGATORS = 2
LISTENERS_PER_AGGREGATOR = 2


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def factory(pg_url):
    _, session_factory = create_session_factory(pg_url)
    return session_factory


@pytest.fixture(scope="module")
def app(pg_url, factory):
    application = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e511-test-secret",
            kek=make_kek(),
            cors_origins="",
            publish_enabled=False,
        )
    )
    with factory() as db:
        org = Organization(name="e511-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E511", slug="e511")
        db.add(dep)
        db.flush()
        for index in range(AGGREGATORS):
            pod = Pod(deployment_id=dep.id, name=f"e511-pod-{index}")
            db.add(pod)
            db.flush()
            aggregator = Aggregator(
                pod_id=pod.id,
                aggregator_uuid=f"e511-agg-{index}",
                name=f"e511-agg-{index}",
            )
            db.add(aggregator)
            db.flush()
            for leaf in range(LISTENERS_PER_AGGREGATOR):
                db.add(
                    Listener(
                        mac=f"02:E5:11:00:{index:02X}:{leaf:02X}",
                        aggregator_id=aggregator.id,
                        deployment_id=dep.id,
                        name=f"e511-listener-{index}-{leaf}",
                    )
                )
        for role, email in ROLE_EMAILS.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            scope = None if role in (Role.OWNER, Role.VIEWER) else dep.id
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
            db.add(user)
        db.commit()
        application.state.e511_deployment_id = dep.id
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e511_deployment_id


def client_for(app, role: Role) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": ROLE_EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def stack_url(dep_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/deployments/{dep_id}/services/stack"


@pytest.fixture
def owner(app) -> TestClient:
    return client_for(app, Role.OWNER)


@pytest.fixture(autouse=True)
def clean(app, dep_id):
    def reset():
        store = app.state.secret_store
        with app.state.session_factory() as db:
            for row in load_services(db, dep_id):
                db.delete(row)
            db.commit()
            for name in db.scalars(
                select(Secret.name).where(Secret.name.like(f"deployment:{dep_id}:%"))
            ).all():
                store.delete(name)
            db.execute(sql_delete(AuditLog).where(AuditLog.action.like("services.stack.%")))
            db.execute(sql_delete(ConfigRevision))
            # **The projection cache too.** `entity_override` holds the
            # deployment's projected service keys, and leaving it behind makes
            # the NEXT test's first projection a no-op — which silently turns
            # "how many revisions did this rotation mint" into a question about
            # test order. That cost a wrong conclusion once already.
            db.execute(sql_delete(EntityOverride).where(EntityOverride.entity_id == str(dep_id)))
            db.commit()

    reset()
    yield
    reset()


def generate(owner: TestClient, dep_id: uuid.UUID, **body):
    return owner.post(stack_url(dep_id), json=body, headers=csrf(owner))


def rotate(owner: TestClient, dep_id: uuid.UUID, **body):
    return owner.post(f"{stack_url(dep_id)}/rotate", json=body, headers=csrf(owner))


def secret_values(app, dep_id: uuid.UUID) -> dict[str, str]:
    """Every stack-owned secret, by name. Read through the store so the
    comparison is of plaintexts and not of ciphertexts, which are salted."""
    store = app.state.secret_store
    values = {}
    for item in STACK_SECRET_ITEMS:
        name = stack_secret_name(dep_id, item)
        if store.exists(name):
            values[name] = store.get(name)
    with app.state.session_factory() as db:
        for row in load_services(db, dep_id):
            for field_name in row.secret_names.values():
                if store.exists(field_name):
                    values[field_name] = store.get(field_name)
            if row.password_secret_name and store.exists(row.password_secret_name):
                values[row.password_secret_name] = store.get(row.password_secret_name)
    return values


# --- Rotation replaces every credential --------------------------------------


def test_rotation_replaces_every_credential(owner, app, dep_id):
    """**The acceptance: the old credentials are absent from SecretStore.**

    Compared by VALUE and over the same names, because regeneration overwrites
    deterministic names rather than writing new ones — "absent" here means no
    name still holds the old plaintext, not that a name disappeared.
    """
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    before = secret_values(app, dep_id)
    assert before, "generation stored nothing to rotate"

    assert (
        rotate(owner, dep_id, hostname="broker.example", include_object_storage=True).status_code
        == 200
    )
    after = secret_values(app, dep_id)

    old_values = set(before.values())
    still_live = {name for name, value in after.items() if value in old_values}
    assert not still_live, f"these secrets survived the rotation: {sorted(still_live)}"


def test_the_certificate_authority_is_rotated_too(owner, app, dep_id):
    """The CA is a credential like any other. Rotating everything except the
    thing that signs the broker's identity would leave a stack whose oldest
    secret is its most valuable one."""
    generate(owner, dep_id, hostname="broker.example")
    before = secret_values(app, dep_id)[stack_secret_name(dep_id, "ca_cert")]
    rotate(owner, dep_id, hostname="broker.example")
    after = secret_values(app, dep_id)[stack_secret_name(dep_id, "ca_cert")]
    assert before != after


def test_rotating_a_deployment_with_no_stack_is_a_404_and_not_a_generate(owner, dep_id):
    """An operator who meant to rotate one deployment and typed another's id
    gets an error, not a brand-new set of credentials for the wrong one."""
    response = rotate(owner, dep_id, hostname="broker.example")
    assert response.status_code == 404, response.text


# --- One revision per Aggregator, zero per Listener --------------------------


def test_rotation_mints_one_revision_per_aggregator_and_none_per_listener(owner, app, dep_id):
    """**The acceptance, with real numbers.** Spec 5.4 keeps the service keys
    off Listener-bound config, so a Listener's rendered snapshot does not change
    and its plan entry is a no-op. Two Aggregators and four Listeners here, so
    a bug that minted one revision per ENTITY would read 6 and fail."""
    generate(owner, dep_id, hostname="broker.example")
    response = rotate(owner, dep_id, hostname="broker.example")
    assert response.status_code == 200, response.text
    assert response.json()["revisions"] == AGGREGATORS

    with app.state.session_factory() as db:
        rows = db.scalars(select(ConfigRevision)).all()
        targets = {row.target_type for row in rows}
    assert targets == {"aggregator"}, f"a revision was minted for {targets - {'aggregator'}}"


# --- The order that looks wrong and is not -----------------------------------


def test_a_failed_reverification_still_publishes_and_reports_degraded(owner, app, dep_id):
    """**The claim the phase document singles out, and the one most likely to
    be "fixed" into a defect.**

    Nothing is listening at `broker.example`, so re-verification fails — which
    is exactly the state an operator is in between rotating and restarting
    their stack. The devices must be told the new credentials ANYWAY: the old
    ones are already gone from SecretStore and will stop being accepted the
    moment the stack restarts, so a device that was not told is a device locked
    out. Publishing only on success would guarantee that outcome in precisely
    the case where it hurts.
    """
    generate(owner, dep_id, hostname="broker.example")
    response = rotate(owner, dep_id, hostname="broker-moved.example")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["verified"] is False
    assert body["services_status"] == "degraded"
    # Published anyway: the revisions exist and were minted by this call.
    assert body["revisions"] == AGGREGATORS
    with app.state.session_factory() as db:
        assert db.scalars(select(ConfigRevision)).all()


def test_a_secret_only_rotation_still_reaches_every_aggregator(owner, app, dep_id):
    """**The claim that needed a catalog key to be true** (D146).

    A device's desired snapshot carries secret MARKERS and never plaintext
    (spec 5.4, 8; D51, D138), and a marker is a SecretStore NAME — the same
    string before and after a rotation. So rotating every credential while
    leaving the hostname alone changes nothing a device can see: measured, it
    minted ZERO revisions, and rotating to a different hostname minted two. The
    projection was working; there was simply nothing to say.

    `services.credentials_generation` is what there is to say. It is a
    non-secret counter, bumped in the same transaction as the credentials, so a
    rotation that changes only secrets still produces one revision per
    Aggregator — and spec 16.3's "rotation is a config revision, not a manual
    redistribution" becomes true of every device rather than only of a
    deployment that happened to move house.
    """
    generate(owner, dep_id, hostname="broker.example")
    # The first rotation lands the projection; the second changes ONLY
    # credentials, which is the case under test.
    assert rotate(owner, dep_id, hostname="broker.example").json()["revisions"] == AGGREGATORS
    with app.state.session_factory() as db:
        db.execute(sql_delete(ConfigRevision))
        db.commit()

    assert rotate(owner, dep_id, hostname="broker.example").json()["revisions"] == AGGREGATORS
    with app.state.session_factory() as db:
        rows = db.scalars(select(ConfigRevision)).all()
        assert {row.target_type for row in rows} == {"aggregator"}


def test_the_generation_counter_advances_once_per_generation(owner, app, dep_id):
    """A count, not a timestamp: two renders of one generation must be
    byte-identical (fixed choice 7) and a clock is not. It advances on
    generation as well as rotation, because both replace every credential."""
    generate(owner, dep_id, hostname="broker.example")
    with app.state.session_factory() as db:
        first = db.get(Deployment, dep_id).services_credentials_generation
    rotate(owner, dep_id, hostname="broker.example")
    with app.state.session_factory() as db:
        second = db.get(Deployment, dep_id).services_credentials_generation
    assert second == first + 1


def test_the_generation_reaches_the_aggregators_snapshot(owner, app, dep_id):
    """It is only worth a catalog key if a device actually receives it, so this
    reads the minted revision's snapshot rather than the override row."""
    generate(owner, dep_id, hostname="broker.example")
    rotate(owner, dep_id, hostname="broker.example")
    with app.state.session_factory() as db:
        generation = db.get(Deployment, dep_id).services_credentials_generation
        revisions = db.scalars(select(ConfigRevision)).all()
        snapshots = [row.snapshot for row in revisions if row.target_type == "aggregator"]
    assert snapshots
    for snapshot in snapshots:
        assert snapshot["services.credentials_generation"] == generation


def test_listeners_never_receive_the_generation_key(owner, app, dep_id):
    """Spec 5.4 keeps the service keys off Listener-bound config, and the
    counter is one of them — a Listener that received it would start minting a
    revision on every rotation, which is exactly the "zero per Listener" half
    of the acceptance."""
    generate(owner, dep_id, hostname="broker.example")
    rotate(owner, dep_id, hostname="broker.example")
    with app.state.session_factory() as db:
        listener_revisions = [
            row for row in db.scalars(select(ConfigRevision)).all() if row.target_type == "listener"
        ]
    assert listener_revisions == []


def test_status_is_never_set_optimistically(owner, dep_id):
    """`verified` comes from a real test pass and from nothing else. A rotation
    that assumed its own success would make spec 16.5's provisioning gate a
    statement about intent rather than about reality."""
    generate(owner, dep_id, hostname="broker.example")
    assert rotate(owner, dep_id, hostname="broker.example").json()["services_status"] != "verified"


def test_every_service_carries_a_result_with_a_remedy(owner, dep_id):
    """The operator has to be able to act on a failed rotation. Every failing
    check carries a remedy across this phase, and a rotation is where they are
    read most urgently."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    results = rotate(owner, dep_id, hostname="broker.example", include_object_storage=True).json()[
        "results"
    ]
    assert {result["service_key"] for result in results} == {
        "mqtt",
        "influx",
        "prometheus",
        "grafana",
        "s3",
    }
    for result in results:
        for check in result["checks"]:
            if not check["passed"]:
                assert check["remedy"].strip(), f"{result['service_key']}.{check['name']}"


# --- Permissions and audit ---------------------------------------------------


@pytest.mark.parametrize("role", [Role.FIELD_TECH, Role.VIEWER])
def test_rotation_is_manage_services_only(app, dep_id, owner, role):
    """Fixed choice 9. Rotation replaces every credential the deployment has;
    a Field Tech provisions hardware and a Viewer reads."""
    generate(owner, dep_id, hostname="broker.example")
    client = client_for(app, role)
    response = client.post(
        f"{stack_url(dep_id)}/rotate", json={"hostname": "broker.example"}, headers=csrf(client)
    )
    assert response.status_code == 403, response.text


def test_the_audit_trail_records_the_rotation_and_its_verdict(owner, app, dep_id):
    """Two entries, because they are two facts: what was rotated, and what the
    re-verification then found. Neither carries a credential."""
    generate(owner, dep_id, hostname="broker.example")
    rotate(owner, dep_id, hostname="broker.example")

    with app.state.session_factory() as db:
        actions = [
            row.action
            for row in db.scalars(
                select(AuditLog).where(AuditLog.action.like("services.stack.%"))
            ).all()
        ]
        entries = db.scalars(
            select(AuditLog).where(AuditLog.action == "services.stack.reverify")
        ).all()
    assert "services.stack.rotate" in actions
    assert "services.stack.reverify" in actions
    detail = entries[0].detail
    assert detail["services_status"] == "degraded"
    assert set(detail["outcomes"]) == {"mqtt", "influx", "prometheus", "grafana", "s3"}


def test_no_rotation_response_or_audit_row_carries_a_credential(owner, app, dep_id):
    """The phase's definition-of-done rule at the one endpoint that mints every
    credential a deployment has and then reports on all five."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    response = rotate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    blob = response.text
    for marker in ("BEGIN ", "PRIVATE KEY", "$7$", "$2b$", "$2y$", "apiv3_"):
        assert marker not in blob, f"the rotate response leaked {marker!r}"

    live = secret_values(app, dep_id)
    for value in live.values():
        if len(value) >= 12:
            assert value not in blob, "a stored credential appeared in the rotate response"

    with app.state.session_factory() as db:
        details = [
            row.detail
            for row in db.scalars(
                select(AuditLog).where(AuditLog.action.like("services.stack.%"))
            ).all()
        ]
    audit_blob = repr(details)
    for value in live.values():
        if len(value) >= 12:
            assert value not in audit_blob, "a stored credential appeared in the audit log"


# --- Regeneration turns object storage off -----------------------------------


def test_rotating_without_object_storage_removes_its_row_and_secrets(owner, app, dep_id):
    """A rotation is where an operator changes their mind about object storage.
    The row has to go, or `roll_up` keeps waiting on a service they just said
    they do not run — it reads rows, not intentions."""
    generate(owner, dep_id, hostname="broker.example", include_object_storage=True)
    rotate(owner, dep_id, hostname="broker.example", include_object_storage=False)

    with app.state.session_factory() as db:
        assert "s3" not in {row.service_key for row in load_services(db, dep_id)}
    assert not [name for name in secret_values(app, dep_id) if ":s3_" in name]
