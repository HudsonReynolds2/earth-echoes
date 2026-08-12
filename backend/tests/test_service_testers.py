"""E5.3: the connection-test framework and `POST .../services/test` (spec 16.2).

The five real testers are E5.4a-e. What this suite pins is the framework they
plug into, and it pins the four properties that make the framework worth
having rather than the ones that are easy to assert:

1. **The budgets are real.** A tester dialling a service that accepts the
   connection and then says nothing forever is stopped by its own budget, and
   the whole call is bounded even by a tester that ignores its budget. Both are
   asserted with a clock, because an API request that hangs on a dead
   deployment is the failure mode an endpoint dialling five external systems
   invites.
2. **One tester's failure never costs the other four their verdicts** - S5's
   own caption, and the reason each tester is contained individually.
3. **Every failing check carries a remedy**, asserted table-driven over every
   failure path the framework can produce. A red row with no "what now" is
   what teaches operators to ignore red.
4. **No credential reaches a result, a log record, or the audit row.** The
   crashing tester here raises an exception whose MESSAGE contains a token, on
   purpose: `str(error)` in a failure detail is exactly how a credential ends
   up in an API response, and this suite fails if that is ever how it is
   written.

The black hole is an in-process server that accepts and then stays silent,
rather than a bound-but-unaccepting socket: a listening socket completes the
handshake in the kernel and a full backlog is timing-dependent, so
accept-then-silence is the deterministic way to make a tester hang.
"""

import asyncio
import json
import time
import uuid

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.rbac import Role
from app.main import API_PREFIX, create_app
from app.models import AuditLog, Deployment, Organization, RoleAssignment, User
from app.services import testers as testers_module
from app.services.schemas import InfluxSettings, MqttSettings, S3Settings, secret_name
from app.services.store import upsert_service
from app.services.testers.base import (
    TESTER_OUTCOMES,
    CheckResult,
    ServiceCredentials,
    TestResult,
    resolve_credentials,
    run_testers,
)
from app.settings import Settings

PASSWORD = "correct horse battery staple"  # noqa: S105  (test account, not a credential)

#: The canary the crashing tester puts in its exception message. Named
#: *_CANARY rather than *_TOKEN on purpose: `test_repo_layout`'s
#: SECRET_PATTERNS scanner flags a `TOKEN = "..."` assignment in the tree,
#: and that guard is a definition-of-done item for this phase - the name
#: moves, the guard does not. If this string
#: ever reaches a result, a log record or the audit row, the redaction is
#: broken and this suite is what says so.
LEAKY_CANARY = "PLAINTEXT-leaky-token-3f9c22"  # noqa: S105  (a canary, not a credential)

STORED_CANARY = "PLAINTEXT-stored-influx-token-88ab41"  # noqa: S105
CANDIDATE_CANARY = "PLAINTEXT-candidate-influx-token-12de70"  # noqa: S105


# --- stub testers ------------------------------------------------------------


class PassingTester:
    """The happy path, so the other four verdicts have something real to be."""

    budget_seconds = 5.0

    def __init__(self, service_key: str) -> None:
        self.service_key = service_key

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        return TestResult(
            service_key=self.service_key,
            outcome="pass",
            checks=(
                CheckResult(name="reachable", passed=True, detail="ok", remedy="", elapsed_ms=1),
            ),
        )


class BlackHoleTester:
    """Connects to a server that accepts and then never speaks, and waits for a
    line that will never arrive. Its budget is what stops it."""

    service_key = "influx"
    budget_seconds = 1.0

    def __init__(self, port: int) -> None:
        self.port = port

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            await reader.readline()
        finally:
            writer.close()
        raise AssertionError("unreachable: the budget fires first")


class CrashingTester:
    """Raises something it should have handled, with a credential in the
    message - the exact shape that makes `str(error)` a leak."""

    service_key = "grafana"
    budget_seconds = 5.0

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        raise RuntimeError(f"connection to https://grafana.example?token={LEAKY_CANARY} failed")


class BudgetIgnoringTester:
    """Declares a budget longer than the whole call's, so the whole-call
    backstop is the only thing that can stop it."""

    service_key = "prometheus"
    budget_seconds = 60.0

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        await asyncio.sleep(60)
        raise AssertionError("unreachable: the whole-call budget fires first")


class WrongKeyTester:
    """Returns a result for a service it was not asked about."""

    service_key = "s3"
    budget_seconds = 5.0

    async def run(self, credentials: ServiceCredentials) -> TestResult:
        return TestResult(service_key="influx", outcome="pass")


def creds(service_key: str) -> ServiceCredentials:
    return ServiceCredentials(service_key=service_key, settings={"url": "https://x"}, secrets={})


@pytest.fixture
async def black_hole_port(anyio_backend):
    """A server that accepts the connection and then says nothing, ever.

    Teardown releases the parked handlers and closes their writers BEFORE
    `wait_closed()`, because that call waits for every live handler: a handler
    on a never-resolving await would hang the suite itself, which is the bug
    this fixture exists to provoke in the code under test and nowhere else.
    """
    writers: list[asyncio.StreamWriter] = []
    parked = asyncio.Event()

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        await parked.wait()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        parked.set()
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


# --- 1. the budgets are real -------------------------------------------------


@pytest.mark.anyio
async def test_a_black_holed_service_fails_within_its_own_budget(black_hole_port):
    tester = BlackHoleTester(black_hole_port)
    started = time.monotonic()
    [result] = await run_testers([tester], {"influx": creds("influx")})
    elapsed = time.monotonic() - started

    assert result.outcome == "fail"
    assert result.service_key == "influx"
    assert tester.budget_seconds <= elapsed < tester.budget_seconds + 1.0, (
        f"the per-tester budget did not bound the call: {elapsed:.2f}s "
        f"against a {tester.budget_seconds}s budget"
    )
    assert all(check.remedy for check in result.checks if not check.passed)


@pytest.mark.anyio
async def test_the_whole_call_is_bounded_even_by_a_tester_that_ignores_its_budget():
    """The backstop. `BudgetIgnoringTester` declares 60s; the whole call is
    given 1s and must still answer, with the other tester's verdict intact."""
    started = time.monotonic()
    results = await run_testers(
        [BudgetIgnoringTester(), PassingTester("mqtt")],
        {"prometheus": creds("prometheus"), "mqtt": creds("mqtt")},
        whole_call_budget=1.0,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"the whole-call budget did not bound the call: {elapsed:.2f}s"
    outcomes = {result.service_key: result.outcome for result in results}
    assert outcomes == {"prometheus": "fail", "mqtt": "pass"}


@pytest.mark.anyio
async def test_results_come_back_in_the_order_the_testers_were_given():
    testers = [PassingTester(key) for key in ("mqtt", "influx", "prometheus")]
    results = await run_testers(
        testers, {key: creds(key) for key in ("mqtt", "influx", "prometheus")}
    )
    assert [result.service_key for result in results] == ["mqtt", "influx", "prometheus"]


# --- 2. containment ----------------------------------------------------------


@pytest.mark.anyio
async def test_one_crashing_tester_leaves_the_others_real(black_hole_port):
    testers = [
        PassingTester("mqtt"),
        BlackHoleTester(black_hole_port),
        CrashingTester(),
        PassingTester("s3"),
    ]
    given = {key: creds(key) for key in ("mqtt", "influx", "grafana", "s3")}
    results = await run_testers(testers, given)

    outcomes = {result.service_key: result.outcome for result in results}
    assert outcomes == {"mqtt": "pass", "influx": "fail", "grafana": "fail", "s3": "pass"}


@pytest.mark.anyio
async def test_a_tester_returning_the_wrong_service_key_is_that_service_s_failure():
    """Not silently accepted: a result filed under another service's key would
    overwrite a real verdict with an unrelated one."""
    [result] = await run_testers([WrongKeyTester()], {"s3": creds("s3")})
    assert result.service_key == "s3"
    assert result.outcome == "fail"


@pytest.mark.anyio
async def test_a_service_with_nothing_configured_is_not_configured_rather_than_failed():
    """Spec 16.2 makes S3 conditionally required and a wizard on step two has
    not entered Grafana yet. Red here is the red that teaches people to ignore
    red."""
    [result] = await run_testers([PassingTester("grafana")], {"grafana": None})
    assert result.outcome == "not_configured"
    assert result.outcome in TESTER_OUTCOMES
    assert all(check.remedy for check in result.checks if not check.passed)


# --- 3. every failing check carries a remedy ---------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    ["timeout", "crash", "not_configured", "wrong_key", "whole_call_timeout"],
)
async def test_every_failure_path_produces_a_remedy(path, black_hole_port):
    """Table-driven over every failure the FRAMEWORK can produce. E5.4a-e add
    their own paths and extend this table."""
    if path == "timeout":
        results = await run_testers([BlackHoleTester(black_hole_port)], {"influx": creds("influx")})
    elif path == "crash":
        results = await run_testers([CrashingTester()], {"grafana": creds("grafana")})
    elif path == "not_configured":
        results = await run_testers([PassingTester("s3")], {"s3": None})
    elif path == "wrong_key":
        results = await run_testers([WrongKeyTester()], {"s3": creds("s3")})
    else:
        results = await run_testers(
            [BudgetIgnoringTester()], {"prometheus": creds("prometheus")}, whole_call_budget=1.0
        )

    failing = [check for result in results for check in result.checks if not check.passed]
    assert failing, f"the {path} path produced no failing check to carry a remedy"
    for check in failing:
        assert check.remedy.strip(), f"{path}: check {check.name!r} failed with no remedy"
        assert check.detail.strip(), f"{path}: check {check.name!r} failed with no detail"


# --- 4. nothing leaks --------------------------------------------------------


@pytest.mark.anyio
async def test_a_crash_names_the_exception_type_and_never_its_message(caplog):
    """`str(error)` is how a credential reaches an API response - httpx puts
    the request URL in its messages and a URL can carry a token."""
    with caplog.at_level("DEBUG"):
        [result] = await run_testers([CrashingTester()], {"grafana": creds("grafana")})

    blob = json.dumps(
        {
            "service_key": result.service_key,
            "outcome": result.outcome,
            "checks": [check.__dict__ for check in result.checks],
        }
    )
    assert LEAKY_CANARY not in blob, "the crash reason leaked the exception message"
    assert "RuntimeError" in blob, "the crash reason should name the exception TYPE"
    for record in caplog.records:
        assert LEAKY_CANARY not in record.getMessage()


def test_credentials_never_render_their_secrets():
    """`ServiceCredentials` follows `BrokerCoordinates`' precedent (D66): a
    stray `%r` or f-string in a later tester must not leak a token."""
    holder = ServiceCredentials(
        service_key="influx",
        settings={"url": "https://influx.example", "database": "recordings"},
        secrets={"token": LEAKY_CANARY},
    )
    assert LEAKY_CANARY not in repr(holder)
    assert LEAKY_CANARY not in str(holder)
    assert LEAKY_CANARY not in f"{holder}" and LEAKY_CANARY not in f"{holder!r}"
    # and it still carries the value where the tester needs it
    assert holder.secrets["token"] == LEAKY_CANARY


# --- credential resolution ---------------------------------------------------


class FakeStore:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str:
        return self.values[name]


def test_resolve_returns_none_when_there_is_nothing_to_dial():
    assert resolve_credentials("grafana", None, None, FakeStore({}).get) is None


def test_resolve_reads_the_stored_row_when_no_candidate_is_submitted():
    dep = uuid.uuid4()
    row = _row(
        "influx",
        config={"url": "https://influx.example", "database": "recordings"},
        secret_names={"token": secret_name(dep, "influx", "token")},
    )
    resolved = resolve_credentials(
        "influx", row, None, FakeStore({secret_name(dep, "influx", "token"): STORED_CANARY}).get
    )
    assert resolved is not None
    assert resolved.settings == {"url": "https://influx.example", "database": "recordings"}
    assert resolved.secrets == {"token": STORED_CANARY}


def test_a_candidate_beats_the_stored_row_and_the_sentinel_reaches_back():
    """Spec 16.2's "validates each entry before accepting it" - the operator
    edits the URL, leaves the token alone, and the test dials the new URL with
    the old token."""
    dep = uuid.uuid4()
    stored_name = secret_name(dep, "influx", "token")
    row = _row(
        "influx",
        config={"url": "https://old.example", "database": "old"},
        secret_names={"token": stored_name},
    )

    kept = resolve_credentials(
        "influx",
        row,
        InfluxSettings(
            url="https://new.example", database="recordings", token={"$secret_set": True}
        ),
        FakeStore({stored_name: STORED_CANARY}).get,
    )
    assert kept is not None
    assert kept.settings == {"url": "https://new.example", "database": "recordings"}
    assert kept.secrets == {"token": STORED_CANARY}

    replaced = resolve_credentials(
        "influx",
        row,
        InfluxSettings(url="https://new.example", database="recordings", token=CANDIDATE_CANARY),
        FakeStore({stored_name: STORED_CANARY}).get,
    )
    assert replaced is not None
    assert replaced.secrets == {"token": CANDIDATE_CANARY}


def test_an_unreadable_secret_is_skipped_rather_than_raised(caplog):
    """A row naming a secret the store has lost is a real state. Raising here
    would replace four honest verdicts with a 500."""
    dep = uuid.uuid4()
    missing = secret_name(dep, "influx", "token")
    row = _row(
        "influx",
        config={"url": "https://influx.example", "database": "d"},
        secret_names={"token": missing},
    )
    with caplog.at_level("WARNING"):
        resolved = resolve_credentials("influx", row, None, FakeStore({}).get)
    assert resolved is not None
    assert resolved.secrets == {}
    assert any(missing in record.getMessage() for record in caplog.records)


def test_the_broker_password_resolves_from_its_own_column():
    dep = uuid.uuid4()
    name = secret_name(dep, "mqtt", "password")
    row = _row(
        "mqtt", host="broker.example", port=8883, username="platform", password_secret_name=name
    )
    resolved = resolve_credentials("mqtt", row, None, FakeStore({name: "broker-pw"}).get)
    assert resolved is not None
    assert resolved.settings["host"] == "broker.example"
    assert resolved.settings["port"] == 8883
    assert resolved.secrets == {"password": "broker-pw"}


def test_a_candidate_for_a_service_with_no_stored_row_needs_no_row():
    resolved = resolve_credentials(
        "s3",
        None,
        S3Settings(bucket="audio", access_key="ak", secret_key="sk"),
        FakeStore({}).get,
    )
    assert resolved is not None
    assert resolved.settings["bucket"] == "audio"
    assert resolved.secrets == {"access_key": "ak", "secret_key": "sk"}


def test_a_sentinel_with_no_stored_row_resolves_to_no_secret():
    """Not an error here (E5.2's PUT is where that is a 422): the tester fails
    on the resulting auth error with a real remedy, which tells the operator
    more than a 422 about a field they did not submit."""
    resolved = resolve_credentials(
        "mqtt",
        None,
        MqttSettings(host="h", port=8883, username="u", password={"$secret_set": True}),
        FakeStore({}).get,
    )
    assert resolved is not None
    assert resolved.secrets == {}


def _row(service_key: str, **kwargs):
    from app.models import DeploymentService

    row = DeploymentService(deployment_id=uuid.uuid4(), service_key=service_key)
    row.config = kwargs.pop("config", {})
    row.secret_names = kwargs.pop("secret_names", {})
    for name, value in kwargs.items():
        setattr(row, name, value)
    return row


# --- the endpoint ------------------------------------------------------------


OWNER = "e53-owner@example.com"
TECH = "e53-tech@example.com"


@pytest.fixture(scope="module")
def pg_url():
    with ephemeral_postgres() as url:
        yield url


@pytest.fixture(scope="module")
def app(pg_url):
    application = create_app(
        Settings(
            database_url=pg_url,
            session_secret="e53-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    with application.state.session_factory() as db:
        org = Organization(name="e53-org")
        db.add(org)
        db.flush()
        dep = Deployment(organization_id=org.id, name="E53", slug="e53")
        db.add(dep)
        db.flush()
        for email, role in ((OWNER, Role.OWNER), (TECH, Role.FIELD_TECH)):
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role.value, deployment_id=None))
            db.add(user)
        upsert_service(
            db,
            dep.id,
            "influx",
            config={"url": "https://influx.example", "database": "recordings"},
            secret_names={"token": secret_name(dep.id, "influx", "token")},
        )
        # Grafana is stored too, so the crashing tester has credentials to be
        # given and reaches `fail` rather than `not_configured` - the two are
        # different facts and the endpoint tests must exercise the right one.
        upsert_service(
            db,
            dep.id,
            "grafana",
            config={"base_url": "https://grafana.example"},
            secret_names={"service_account_token": secret_name(dep.id, "grafana", "token")},
        )
        db.commit()
        application.state.e53_deployment_id = dep.id
    dep_id = application.state.e53_deployment_id
    application.state.secret_store.put(secret_name(dep_id, "influx", "token"), STORED_CANARY)
    application.state.secret_store.put(secret_name(dep_id, "grafana", "token"), STORED_CANARY)
    return application


def _client(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    assert (
        client.post(
            f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD}
        ).status_code
        == 200
    )
    return client


def _test_url(app) -> str:
    return f"{API_PREFIX}/deployments/{app.state.e53_deployment_id}/services/test"


@pytest.fixture
def registry(monkeypatch):
    """The real registry is empty until E5.4a-e. Tests install stubs into the
    MODULE attribute, which is exactly how the endpoint reads it."""
    stubs: dict[str, object] = {}
    monkeypatch.setattr(testers_module, "REGISTRY", stubs)
    return stubs


@pytest.mark.integration
def test_the_endpoint_reports_nothing_while_no_tester_is_registered(app, registry):
    """E5.3 ships the framework and E5.4a-e ship the testers. An empty result
    list is the honest answer; inventing verdicts nothing computed is not.

    **Amended by E5.4a**, which registered the first real tester. This test read
    the LIVE `REGISTRY` and asserted it was empty, so what it actually pinned
    was "nobody has written a tester yet" - a fact with a scheduled expiry date
    that says nothing about the endpoint. It now pins the empty stub registry,
    which is what the name claims: given nothing registered, the endpoint
    invents nothing. That property still has to hold after E5.4b-e, and now it
    is the one being tested.
    """
    client = _client(app, OWNER)
    response = client.post(
        _test_url(app), json={}, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"] == []


@pytest.mark.integration
def test_the_endpoint_runs_registered_testers_over_stored_credentials(app, registry):
    registry["influx"] = PassingTester("influx")
    registry["grafana"] = CrashingTester()
    client = _client(app, OWNER)
    response = client.post(
        _test_url(app), json={}, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    outcomes = {result["service_key"]: result["outcome"] for result in body["results"]}
    assert outcomes == {"influx": "pass", "grafana": "fail"}
    assert LEAKY_CANARY not in response.text
    assert STORED_CANARY not in response.text
    for result in body["results"]:
        for check in result["checks"]:
            if not check["passed"]:
                assert check["remedy"].strip()


@pytest.mark.integration
def test_a_candidate_body_is_tested_without_being_saved(app, registry):
    """Spec 16.2: validated with a live connection test BEFORE being accepted.
    Nothing about the stored row may change."""
    seen: dict[str, ServiceCredentials] = {}

    class Recording:
        service_key = "influx"
        budget_seconds = 5.0

        async def run(self, credentials: ServiceCredentials) -> TestResult:
            seen["influx"] = credentials
            return TestResult(service_key="influx", outcome="pass")

    registry["influx"] = Recording()
    client = _client(app, OWNER)
    body = {
        "services": {
            "influx": {
                "url": "https://candidate.example",
                "database": "candidate",
                "token": CANDIDATE_CANARY,
            }
        }
    }
    response = client.post(
        _test_url(app), json=body, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]}
    )
    assert response.status_code == 200, response.text
    assert seen["influx"].settings["url"] == "https://candidate.example"
    assert seen["influx"].secrets["token"] == CANDIDATE_CANARY
    assert CANDIDATE_CANARY not in response.text

    # the stored row is untouched
    stored = client.get(f"{API_PREFIX}/deployments/{app.state.e53_deployment_id}/services").json()
    assert stored["services"]["influx"]["settings"]["url"] == "https://influx.example"


@pytest.mark.integration
def test_testing_is_manage_gated_and_needs_csrf(app):
    tech = _client(app, TECH)
    assert (
        tech.post(
            _test_url(app), json={}, headers={"X-CSRF-Token": tech.cookies["eoe_csrf"]}
        ).status_code
        == 403
    )
    owner = _client(app, OWNER)
    assert owner.post(_test_url(app), json={}).status_code == 403


@pytest.mark.integration
def test_the_test_audit_row_names_outcomes_and_never_a_credential(app, registry):
    registry["influx"] = PassingTester("influx")
    registry["grafana"] = CrashingTester()
    client = _client(app, OWNER)
    assert (
        client.post(
            _test_url(app), json={}, headers={"X-CSRF-Token": client.cookies["eoe_csrf"]}
        ).status_code
        == 200
    )
    dep_id = app.state.e53_deployment_id
    with app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "services.test", AuditLog.entity_id == str(dep_id)
                )
            )
        )
    assert rows
    detail = json.dumps(rows[-1].detail)
    assert '"influx": "pass"' in detail
    assert '"grafana": "fail"' in detail
    for leak in (LEAKY_CANARY, STORED_CANARY, CANDIDATE_CANARY):
        assert leak not in detail
    assert rows[-1].scope == dep_id


def test_every_spec_service_has_a_tester_and_no_others_are_registered():
    """The `REGISTRY` is complete as of E5.4e, and pinned against the vocabulary.

    Added by E5.4e, and it is the assertion `testers/__init__.py`'s docstring
    claims exists. Two directions, both of which have bitten this epic before
    in other forms: a sixth `service_key` reaching `models.SERVICE_KEYS`
    without a tester would silently report nothing for it (D118's shape), and
    a tester registered under a key the database does not accept would never
    run at all.
    """
    from app.models import SERVICE_KEYS
    from app.services import testers as testers_module

    assert tuple(testers_module.REGISTRY) == SERVICE_KEYS

    for key, tester in testers_module.REGISTRY.items():
        assert tester.service_key == key, (
            f"{type(tester).__name__} is registered under {key!r} but reports "
            f"{tester.service_key!r}; the runner keys results by the tester's own value"
        )
        assert tester.budget_seconds > 0
