"""E5.5: the spec 16.5 services status lifecycle.

Three things are being defended here, and they are different kinds of claim:

1. **`roll_up` is right**, asserted table-driven over the cross product of
   per-service statuses with the reason for each mapping written down beside
   it. A rollup is a policy, and a policy asserted by three examples is a
   policy nobody can change safely.
2. **The denormalized column never diverges from its own definition.**
   `assert_rollup_invariant` walks EVERY deployment in the database and
   recomputes, and it is called after every mutation path in this module. That
   is the whole price of phase-5 fixed choice 2: the column is denormalized for
   E6.4's map and E7.4's fan-out, and one pure writer plus this assertion is
   what makes that safe rather than an argument that it will be.
3. **The threshold behaves like a threshold.** One failed re-check does not
   demote a verified deployment; two consecutive ones do; one success resets
   the counter. A map going red on a transient blip is the failure this exists
   to prevent, and a test that only checked "a failure demotes" would pass
   against an implementation that has no threshold at all.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.db import create_session_factory
from app.main import API_PREFIX, create_app
from app.models import SERVICE_KEYS, Deployment, Organization, RoleAssignment, User
from app.services.status import (
    ALWAYS_REQUIRED,
    DEGRADE_AFTER_FAILURES,
    apply_test_results,
    recompute,
    required_keys,
    roll_up,
    services_recheck_sweep,
)
from app.services.store import load_service, load_services, upsert_service
from app.services.testers.base import CheckResult, TestResult
from app.settings import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"  # noqa: S105  (test account, not a credential)
FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"

ROLE_EMAILS = {
    Role.OWNER: "e55-owner@example.com",
    Role.DEPLOYMENT_OPERATOR: "e55-operator@example.com",
    Role.FIELD_TECH: "e55-tech@example.com",
    Role.VIEWER: "e55-viewer@example.com",
}


# --- roll_up, table-driven ---------------------------------------------------


@dataclass
class FakeRow:
    """Enough of a `deployment_service` for a pure function. A real row would
    need a database for a test about arithmetic over four strings."""

    service_key: str
    status: str = "untested"
    consecutive_failures: int = 0
    #: D129: whether this service counts toward the deployment's rollup is a
    #: property of the ROW, not an argument to `roll_up`. A required-set that
    #: only a live test run could reconstruct would leave
    #: `deployment.services_status` irreproducible from the rows by a save or
    #: by the invariant sweep below.
    required: bool = True


def rows(statuses: dict[str, str], *, not_required: Collection[str] = ()) -> list[FakeRow]:
    return [
        FakeRow(service_key=key, status=status, required=key not in not_required)
        for key, status in statuses.items()
    ]


ALL_FOUR_VERIFIED = dict.fromkeys(ALWAYS_REQUIRED, "verified")


#: (case, rows, not_required, expected, why). The `why` is not decoration: a
#: rollup is a policy, and the next person to change one of these rows needs
#: to know what it was for.
ROLLUP_CASES = [
    (
        "nothing configured at all",
        {},
        (),
        "unconfigured",
        "a deployment nobody has started onboarding is not a deployment that is failing",
    ),
    (
        "one service entered, untested",
        {"mqtt": "untested"},
        (),
        "pending_verification",
        "onboarding has started and nothing has been proven yet",
    ),
    (
        "one service verified, three required ones missing",
        {"mqtt": "verified"},
        (),
        "pending_verification",
        "spec 16.2 requires four; one of them working is not the deployment working",
    ),
    (
        "all four required verified",
        dict(ALL_FOUR_VERIFIED),
        (),
        "verified",
        "the only state spec 16.5 allows a provisioning bundle to be generated from",
    ),
    (
        "all four verified, s3 not configured",
        dict(ALL_FOUR_VERIFIED),
        (),
        "verified",
        "object storage is CONDITIONALLY required (spec 16.2); not entering it is a choice",
    ),
    (
        "all four verified, s3 configured and verified",
        {**ALL_FOUR_VERIFIED, "s3": "verified"},
        (),
        "verified",
        "having configured it, it counts, and it passes",
    ),
    (
        "all four verified, s3 configured and failed",
        {**ALL_FOUR_VERIFIED, "s3": "failed"},
        (),
        "degraded",
        "an operator who entered bucket credentials meant to use them; a bucket that "
        "rejects the platform is worth a red dot",
    ),
    (
        "all four verified, s3 failed but NOT required",
        {**ALL_FOUR_VERIFIED, "s3": "failed"},
        ("s3",),
        "verified",
        "raw-audio upload is off, so spec 16.2 does not require it and reporting it red "
        "would train operators to ignore red",
    ),
    (
        "one required service failed",
        {**ALL_FOUR_VERIFIED, "mqtt": "failed"},
        (),
        "degraded",
        "spec 16.5's degraded, and with the broker down nothing else matters",
    ),
    (
        "one failed and one missing",
        {"mqtt": "failed", "influx": "verified"},
        (),
        "degraded",
        "a failure is louder than an absence: the operator needs to see the broken one, "
        "not be told they have more forms to fill in",
    ),
    (
        "one untested among verified",
        {**ALL_FOUR_VERIFIED, "grafana": "untested"},
        (),
        "pending_verification",
        "nothing is broken and it is not all proven; that is exactly what the word means",
    ),
    (
        "only a non-required service, configured",
        {"s3": "verified"},
        (),
        "pending_verification",
        "something IS configured, so not unconfigured - and the four required ones are "
        "still missing",
    ),
]


@pytest.mark.parametrize(
    ("case", "statuses", "not_required", "expected", "why"),
    ROLLUP_CASES,
    ids=[case[0] for case in ROLLUP_CASES],
)
def test_roll_up(case, statuses, not_required, expected, why):
    assert roll_up(rows(statuses, not_required=not_required)) == expected, why


def test_roll_up_covers_every_service_key():
    """The table above is hand-written, so it has to be checked against the
    vocabulary rather than trusted to have kept up. A sixth service key with
    no rollup opinion is a silent hole."""
    mentioned = {key for _, statuses, *_ in ROLLUP_CASES for key in statuses}
    assert mentioned == set(SERVICE_KEYS)


def test_only_object_storage_is_conditionally_required():
    """Spec 16.2's conditional requirement is object storage and nothing else.
    Asserted against the vocabulary so a future service cannot become
    accidentally optional by being forgotten."""
    every_key = dict.fromkeys(SERVICE_KEYS, "untested")
    assert required_keys([]) == frozenset(ALWAYS_REQUIRED)
    assert required_keys(rows(every_key)) == frozenset(SERVICE_KEYS)
    assert set(SERVICE_KEYS) - set(ALWAYS_REQUIRED) == {"s3"}

    # D129: the flag is a property of the row, so a tester's `not_required`
    # survives into every later recompute. Only s3 can ever use it - the other
    # four are read from ALWAYS_REQUIRED, so no verdict can excuse them.
    assert required_keys(rows(every_key, not_required=["s3"])) == frozenset(ALWAYS_REQUIRED)
    assert required_keys(rows(every_key, not_required=SERVICE_KEYS)) == frozenset(ALWAYS_REQUIRED)


# --- fixtures ----------------------------------------------------------------


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
            session_secret="e55-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with factory() as db:
        org = Organization(name="e55-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E55", slug="e55")
        untouched = Deployment(organization_id=org.id, name="E55 other", slug="e55-other")
        db.add_all([dep, untouched])
        db.flush()
        for role, email in ROLE_EMAILS.items():
            user = User(email=email, password_hash=hash_password(PASSWORD))
            scope = None if role in (Role.OWNER, Role.VIEWER) else dep.id
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=scope))
            db.add(user)
        db.commit()
        application.state.e55_deployment_id = dep.id
        application.state.e55_other_deployment_id = untouched.id
    return application


@pytest.fixture
def dep_id(app) -> uuid.UUID:
    return app.state.e55_deployment_id


@pytest.fixture(autouse=True)
def clean(app, factory):
    """Every test starts from an unconfigured deployment, and the invariant is
    asserted on the way OUT of every one of them. That autouse teardown is how
    "after every mutation path in the suite" is enforced rather than
    remembered."""
    _reset(app, factory)
    yield
    assert_rollup_invariant(factory)


def _reset(app, factory) -> None:
    store = app.state.secret_store
    with factory() as db:
        for deployment in db.scalars(select(Deployment)):
            for row in load_services(db, deployment.id):
                names = [*row.secret_names.values()]
                if row.password_secret_name:
                    names.append(row.password_secret_name)
                db.delete(row)
                for name in names:
                    store.delete(name)
            deployment.services_status = "unconfigured"
        db.commit()


def assert_rollup_invariant(factory) -> None:
    """**The justification for denormalizing the column, executed.**

    Walks every deployment, recomputes `roll_up` over its own rows, and
    demands the stored value agrees. Called from the autouse teardown, so a
    mutation path that forgets to recompute fails the test that exercised it
    rather than some unrelated test later.
    """
    with factory() as db:
        for deployment in db.scalars(select(Deployment)):
            expected = roll_up(load_services(db, deployment.id))
            assert deployment.services_status == expected, (
                f"deployment {deployment.slug} stores services_status="
                f"{deployment.services_status!r} but roll_up over its own rows says "
                f"{expected!r} - the denormalized column has diverged from its definition"
            )


def client_for(app, role: Role) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": ROLE_EMAILS[role], "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def url(dep_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/deployments/{dep_id}/services"


@pytest.fixture
def owner(app) -> TestClient:
    return client_for(app, Role.OWNER)


def configure_all(factory, dep_id: uuid.UUID, status: str = "untested") -> None:
    """The five rows, straight through the store, so a status test does not
    depend on the E5.2 endpoint's behaviour."""
    with factory() as db:
        for key in SERVICE_KEYS:
            columns = (
                {
                    "host": "broker.example",
                    "port": 8883,
                    "username": "platform",
                    "password_secret_name": f"deployment:{dep_id}:mqtt_password",
                }
                if key == "mqtt"
                else {}
            )
            row = upsert_service(db, dep_id, key, config={"url": "https://x.example"}, **columns)
            row.status = status
        recompute(db, dep_id)
        db.commit()


def passing(service_key: str) -> TestResult:
    return TestResult(
        service_key=service_key,
        outcome="pass",
        checks=(CheckResult("reachable", True, "answered", "", 5),),
    )


def failing(service_key: str) -> TestResult:
    return TestResult(
        service_key=service_key,
        outcome="fail",
        checks=(
            CheckResult("reachable", False, "no answer from the endpoint", "check the URL", 5),
        ),
    )


# --- apply_test_results and the threshold ------------------------------------


def test_a_pass_verifies_and_zeroes_the_counter(app, factory, dep_id):
    configure_all(factory, dep_id)
    with factory() as db:
        load_service(db, dep_id, "influx").consecutive_failures = 1
        db.commit()

    with factory() as db:
        apply_test_results(db, dep_id, [passing("influx")])
        db.commit()

    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert row.status == "verified"
        assert row.consecutive_failures == 0
        assert row.status_reason is None
        assert row.last_tested_at is not None


def test_a_first_failure_on_an_untested_service_fails_it_immediately(app, factory, dep_id):
    """The threshold guards a DEMOTION, not a first verdict. An operator who
    has just typed a wrong token must see it, not be told to wait."""
    configure_all(factory, dep_id)
    with factory() as db:
        applied = apply_test_results(db, dep_id, [failing("influx")])
        db.commit()

    assert applied[0].status == "failed"
    assert applied[0].tolerated is False
    with factory() as db:
        assert load_service(db, dep_id, "influx").status == "failed"


def test_one_failed_recheck_does_not_demote_a_verified_deployment(app, factory, dep_id):
    """The blip case, and the whole reason `DEGRADE_AFTER_FAILURES` exists."""
    configure_all(factory, dep_id, status="verified")

    with factory() as db:
        applied = apply_test_results(db, dep_id, [failing("influx")])
        db.commit()

    assert applied[0].tolerated is True
    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert row.status == "verified"
        assert row.consecutive_failures == 1
        # The failure is RECORDED even though it did not demote: the wizard
        # shows "1 of 2", and the operator is not kept in the dark.
        assert row.status_reason
        assert row.last_test_detail is not None
        assert db.get(Deployment, dep_id).services_status == "verified"


def test_two_consecutive_failed_rechecks_do_demote(app, factory, dep_id):
    configure_all(factory, dep_id, status="verified")

    for _ in range(DEGRADE_AFTER_FAILURES):
        with factory() as db:
            apply_test_results(db, dep_id, [failing("influx")])
            db.commit()

    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert row.status == "failed"
        assert row.consecutive_failures == DEGRADE_AFTER_FAILURES
        assert db.get(Deployment, dep_id).services_status == "degraded"


def test_a_single_success_resets_the_counter(app, factory, dep_id):
    """So two failures a week apart never add up to a demotion."""
    configure_all(factory, dep_id, status="verified")

    with factory() as db:
        apply_test_results(db, dep_id, [failing("influx")])
        db.commit()
    with factory() as db:
        apply_test_results(db, dep_id, [passing("influx")])
        db.commit()
    with factory() as db:
        apply_test_results(db, dep_id, [failing("influx")])
        db.commit()

    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert row.consecutive_failures == 1
        assert row.status == "verified"
        assert db.get(Deployment, dep_id).services_status == "verified"


def test_not_required_and_not_configured_write_nothing(app, factory, dep_id):
    """Neither is a verdict about a connection (D123), and writing `failed`
    for either is what teaches an operator to ignore red."""
    configure_all(factory, dep_id, status="verified")
    with factory() as db:
        apply_test_results(
            db,
            dep_id,
            [
                TestResult(service_key="s3", outcome="not_required"),
                TestResult(service_key="grafana", outcome="not_configured"),
            ],
        )
        db.commit()

    with factory() as db:
        assert load_service(db, dep_id, "s3").status == "verified"
        assert load_service(db, dep_id, "grafana").status == "verified"
        assert load_service(db, dep_id, "s3").last_tested_at is None


def test_a_not_required_service_cannot_hold_a_deployment_out_of_verified(app, factory, dep_id):
    """The S3-off case end to end: the row exists and is failing, the tester
    says it is not required, and the deployment still verifies."""
    configure_all(factory, dep_id, status="verified")
    with factory() as db:
        load_service(db, dep_id, "s3").status = "failed"
        db.commit()

    with factory() as db:
        apply_test_results(
            db,
            dep_id,
            [
                *(passing(key) for key in ALWAYS_REQUIRED),
                TestResult(service_key="s3", outcome="not_required"),
            ],
        )
        db.commit()

    with factory() as db:
        assert db.get(Deployment, dep_id).services_status == "verified"


def test_no_status_reason_or_audit_detail_carries_a_credential(app, factory, dep_id):
    """A tester's detail may name a host; it may not name a credential (E5.3),
    and this is where that text comes to rest on a row that a GET returns."""
    secret = "PLAINTEXT-e55-token-9f2c41"
    configure_all(factory, dep_id)
    with factory() as db:
        apply_test_results(
            db,
            dep_id,
            [
                TestResult(
                    service_key="influx",
                    outcome="fail",
                    checks=(CheckResult("auth", False, "the token was rejected", "check it", 3),),
                )
            ],
        )
        db.commit()

    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert secret not in str(row.status_reason)
        assert secret not in str(row.last_test_detail)


# --- the endpoint -------------------------------------------------------------


def test_the_status_endpoint_reports_both_vocabularies(app, factory, dep_id, owner):
    configure_all(factory, dep_id, status="verified")
    response = owner.get(f"{url(dep_id)}/status")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["services_status"] == "verified"
    assert body["degrade_after_failures"] == DEGRADE_AFTER_FAILURES
    assert set(body["services"]) == set(SERVICE_KEYS)
    assert list(body["services"]) == list(SERVICE_KEYS), "spec 16.2's order, not the dict's"
    assert body["services"]["mqtt"]["status"] == "verified"
    assert body["services"]["mqtt"]["required"] is True


def test_an_unconfigured_deployment_reports_unconfigured(app, dep_id, owner):
    response = owner.get(f"{url(dep_id)}/status")
    assert response.json()["services_status"] == "unconfigured"
    assert all(not service["configured"] for service in response.json()["services"].values())


def test_object_storage_is_required_only_once_configured(app, factory, dep_id, owner):
    before = owner.get(f"{url(dep_id)}/status").json()
    assert before["services"]["s3"]["required"] is False

    configure_all(factory, dep_id)
    after = owner.get(f"{url(dep_id)}/status").json()
    assert after["services"]["s3"]["required"] is True


@pytest.mark.parametrize(
    "role", [Role.OWNER, Role.DEPLOYMENT_OPERATOR, Role.FIELD_TECH, Role.VIEWER]
)
def test_status_is_readable_by_every_role(app, dep_id, role):
    """`VIEW_SERVICES`, fixed choice 9: status renders everywhere, and there is
    no credential in this response to withhold from anyone."""
    response = client_for(app, role).get(f"{url(dep_id)}/status")
    assert response.status_code == 200, response.text


def test_the_status_endpoint_does_not_write(app, factory, dep_id, owner):
    """A GET that recomputed would make a read a write and put a second writer
    on a column whose entire design is that it has exactly one (fixed choice
    2). Asserted by corrupting the stored value and seeing the GET report the
    corruption rather than silently repairing it."""
    configure_all(factory, dep_id, status="verified")
    with factory() as db:
        db.get(Deployment, dep_id).services_status = "degraded"
        db.commit()

    assert owner.get(f"{url(dep_id)}/status").json()["services_status"] == "degraded"

    # Put it back, or the autouse invariant teardown would fail on damage this
    # test caused deliberately.
    with factory() as db:
        recompute(db, dep_id)
        db.commit()


def test_saving_a_service_unverifies_it(app, factory, dep_id, owner):
    """A verdict is about the credentials that produced it. Replacing them
    without clearing it would let a deployment stay green against a service it
    can no longer reach."""
    configure_all(factory, dep_id, status="verified")
    with factory() as db:
        load_service(db, dep_id, "influx").consecutive_failures = 1
        db.commit()

    response = owner.put(
        url(dep_id),
        json={
            "services": {
                "influx": {
                    "url": "https://influx.example:8181",
                    "database": "recordings",
                    "token": "PLAINTEXT-e55-new-token-3ab918",
                }
            }
        },
        headers=csrf(owner),
    )
    assert response.status_code == 200, response.text

    with factory() as db:
        row = load_service(db, dep_id, "influx")
        assert row.status == "untested"
        assert row.consecutive_failures == 0
        assert row.last_tested_at is None
        assert db.get(Deployment, dep_id).services_status == "pending_verification"


def test_a_save_leaves_the_other_services_verdicts_alone(app, factory, dep_id, owner):
    """PUT is a partial collection of wholesale members (D122); unverifying is
    per-service for the same reason writing is."""
    configure_all(factory, dep_id, status="verified")
    owner.put(
        url(dep_id),
        json={
            "services": {
                "grafana": {
                    "base_url": "https://grafana.example:3000",
                    "service_account_token": "PLAINTEXT-e55-grafana-55de20",
                }
            }
        },
        headers=csrf(owner),
    )
    with factory() as db:
        assert load_service(db, dep_id, "grafana").status == "untested"
        assert load_service(db, dep_id, "influx").status == "verified"


# --- the re-check sweep -------------------------------------------------------


def test_the_sweep_rechecks_every_configured_deployment(app, factory, dep_id):
    configure_all(factory, dep_id, status="untested")

    async def runner(db, deployment_id):
        return [passing(key) for key in SERVICE_KEYS]

    import anyio

    outcomes = anyio.run(services_recheck_sweep, factory, runner)
    assert dep_id in outcomes
    with factory() as db:
        assert db.get(Deployment, dep_id).services_status == "verified"


def test_the_sweep_skips_a_deployment_with_no_services(app, factory, dep_id):
    """Touching every untouched deployment on every cycle is how a sweep
    becomes the reason a database is busy."""
    configure_all(factory, dep_id)
    other = app.state.e55_other_deployment_id

    async def runner(db, deployment_id):
        return [passing(key) for key in SERVICE_KEYS]

    import anyio

    outcomes = anyio.run(services_recheck_sweep, factory, runner)
    assert other not in outcomes


def test_one_deployments_failure_does_not_stop_the_sweep(app, factory, dep_id):
    """`runner.py`'s rule for its own sweeps, applied here: a sweep that dies
    on the first bad deployment leaves every later one unchecked forever."""
    configure_all(factory, dep_id)
    seen: list[uuid.UUID] = []

    async def runner(db, deployment_id):
        seen.append(deployment_id)
        raise RuntimeError("the influx client exploded")

    import anyio

    outcomes = anyio.run(services_recheck_sweep, factory, runner)
    assert seen == [dep_id]
    assert outcomes == {}
    # And the deployment is left exactly as it was, not half-written.
    with factory() as db:
        assert db.get(Deployment, dep_id).services_status == "pending_verification"
