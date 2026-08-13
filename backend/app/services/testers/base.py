"""The connection-test framework (task E5.3; spec 16.2, 16.5).

Spec 16.2 gives every service a live connection test and says the platform
"validates each entry with a live connection test **before accepting it**".
Two things follow, and both shape this module:

* the runner must work against **candidate** credentials the operator has just
  typed and not yet saved, as well as against stored ones - a tester that can
  only read `deployment_service` cannot implement that sentence;
* a failure has to say **what to do about it**. `CheckResult.remedy` is not
  decoration: S5's whole premise is that an operator reads the result and
  fixes their broker, so a failing check with an empty remedy is a defect the
  suite fails on.

**Timeouts are budgets, not hopes.** Each tester declares `budget_seconds` and
the runner enforces it with `asyncio.wait_for`; the whole call has its own
shorter-than-the-client's-patience budget on top. An API request that hangs on
a dead deployment is precisely the failure mode an endpoint that dials five
external systems invites, so the bound is structural: the endpoint cannot
outlast `WHOLE_CALL_BUDGET_SECONDS` no matter what a tester does.

**One tester's failure never blocks the other four** (S5's own caption). Every
tester runs concurrently and is individually contained: a timeout, an
unexpected exception, a missing credential each become that service's `fail`
and leave the other verdicts real.

**Nothing here writes status.** `deployment_service.status` and
`deployment.services_status` are E5.5's (`apply_test_results`, `roll_up`);
this module computes evidence and returns it.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.models import DeploymentService
from app.services.schemas import SERVICE_SCHEMAS, KeepSecret, ServiceSettings

logger = logging.getLogger(__name__)

#: What one tester can conclude.
#:
#: `not_required` and `not_configured` are deliberately NOT `fail`. Spec 16.2
#: makes object storage conditionally required, and a wizard on step two has
#: not entered Grafana yet; reporting either as red would train operators to
#: ignore red, which is the one thing a verification screen cannot afford.
#: E5.5 maps these onto the spec 16.2 per-service status vocabulary
#: (`untested` / `verified` / `failed`) - they are two vocabularies on purpose,
#: because "I did not run" and "I ran and it failed" are different facts.
TesterOutcome = Literal["pass", "fail", "not_required", "not_configured"]

TESTER_OUTCOMES: tuple[str, ...] = ("pass", "fail", "not_required", "not_configured")

#: Default per-tester wall-clock budget. A tester may declare its own; E5.4b's
#: container round trip and E5.4a's broker handshake are not the same shape.
DEFAULT_TESTER_BUDGET_SECONDS = 10.0

#: The bound on the whole endpoint. Longer than any one tester (they run
#: concurrently, so the expected cost is the slowest, not the sum) and short
#: enough that a caller gets an answer rather than a socket timeout.
WHOLE_CALL_BUDGET_SECONDS = 25.0


@dataclass(frozen=True)
class CheckResult:
    """One assertion a tester made, and what to do if it failed.

    `remedy` is required on every failing check and is asserted non-empty
    across the suite. `detail` is operator-facing text and is subject to the
    same rule as every other string here: **it may name a host, a port, a
    database or a status code, and never a credential.**
    """

    name: str
    passed: bool
    detail: str
    remedy: str
    elapsed_ms: int


@dataclass(frozen=True)
class TestResult:
    #: pytest collects anything named Test*; this is a result type, not a test
    #: case. The name is the phase document's, so the marker moves rather than
    #: the name.
    __test__ = False

    service_key: str
    outcome: TesterOutcome
    checks: tuple[CheckResult, ...] = ()


@dataclass(frozen=True)
class ServiceCredentials:
    """What a tester needs to dial one service, with the plaintext resolved.

    This is the one object in the phase that legitimately holds credentials in
    memory, so it follows `BrokerCoordinates`' precedent exactly (E3.2, D66):
    `secrets` is `repr=False` and `__str__` names only the service and its
    non-secret settings, so a stray `%r` or an f-string in a later tester
    cannot leak a token into a log line. Do not remove either.
    """

    service_key: str
    settings: Mapping[str, Any]
    secrets: Mapping[str, str] = field(repr=False, default_factory=dict)
    #: Which deployment this is, for the one tester whose target topic is a
    #: function of it. E5.4a builds its reserved leaf through
    #: `contracts.mqtt.deployment_root`, so the round trip lands inside the
    #: single grant the platform account holds (`topic readwrite eoe/{slug}/#`)
    #: and a broker whose ACL is cut correctly passes without widening it.
    #: Optional because the other four dial a URL that carries no deployment
    #: identity at all, and a required field four testers pass through
    #: untouched is a field that eventually gets passed wrong.
    deployment_id: uuid.UUID | None = None
    deployment_slug: str | None = None

    def __str__(self) -> str:
        return f"{self.service_key}({', '.join(sorted(self.settings))})"


@runtime_checkable
class ServiceTester(Protocol):
    """What E5.4a-e implement. One per spec 16.2 service.

    `run` must not raise for an ordinary failure - it returns a `fail`
    `TestResult` whose checks explain it. The runner contains anything it
    raises anyway, because "the Grafana tester has a bug" must not become
    "the operator cannot see whether their broker works".
    """

    service_key: str
    budget_seconds: float

    async def run(self, credentials: ServiceCredentials) -> TestResult: ...


def _timed_out(service_key: str, budget: float, elapsed_ms: int) -> TestResult:
    return TestResult(
        service_key=service_key,
        outcome="fail",
        checks=(
            CheckResult(
                name="reachable",
                passed=False,
                detail=f"no answer within the {budget:g}s budget for this service",
                remedy=(
                    "check that the host and port are reachable from the platform host and "
                    "that no firewall is dropping the connection; spec 16.2 requires the "
                    "services to be reachable from the platform"
                ),
                elapsed_ms=elapsed_ms,
            ),
        ),
    )


def _crashed(service_key: str, error: BaseException, elapsed_ms: int) -> TestResult:
    """A tester raised something it should have handled.

    **The reason is the exception's TYPE and never its text.** A `str(error)`
    here is how a credential reaches an API response: httpx puts the request
    URL in its messages, and a URL can carry a token in a query string. The
    type is enough to tell an operator this is a platform bug rather than
    their broker, which is the only thing they can act on.
    """
    logger.exception("service tester for %s raised", service_key)
    return TestResult(
        service_key=service_key,
        outcome="fail",
        checks=(
            CheckResult(
                name="tester",
                passed=False,
                detail=f"the {service_key} tester failed unexpectedly ({type(error).__name__})",
                remedy=(
                    "this is a platform defect rather than a problem with your service; "
                    "retry, and report it with the request id from the response header"
                ),
                elapsed_ms=elapsed_ms,
            ),
        ),
    )


async def _run_one(tester: ServiceTester, credentials: ServiceCredentials) -> TestResult:
    """One tester, contained: its own budget, its own failures, no exceptions
    out. Every path returns a `TestResult` for `tester.service_key`."""
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = await asyncio.wait_for(tester.run(credentials), tester.budget_seconds)
    except TimeoutError:
        return _timed_out(tester.service_key, tester.budget_seconds, elapsed_ms())
    except asyncio.CancelledError:
        # The whole-call budget expired. Reported as a timeout rather than
        # swallowed, and NOT re-raised: this task is one of five and the
        # caller wants the other four's verdicts.
        return _timed_out(tester.service_key, tester.budget_seconds, elapsed_ms())
    except Exception as error:  # noqa: BLE001  (containment is the point)
        return _crashed(tester.service_key, error, elapsed_ms())
    if result.service_key != tester.service_key:
        return _crashed(
            tester.service_key,
            ValueError(f"tester returned a result for {result.service_key!r}"),
            elapsed_ms(),
        )
    return result


async def run_testers(
    testers: Sequence[ServiceTester],
    credentials: Mapping[str, ServiceCredentials | None],
    *,
    whole_call_budget: float = WHOLE_CALL_BUDGET_SECONDS,
) -> list[TestResult]:
    """Run every tester concurrently and return one result per tester, in the
    order the testers were given.

    A service whose `credentials` entry is `None` is `not_configured` and its
    tester is never dialled - there is nothing to dial it with, and inventing
    a `fail` for a service the operator has not reached yet is the red that
    teaches people to ignore red.

    The whole-call budget is a backstop over the per-tester ones: the endpoint
    returns within it even if a tester ignores its own budget by blocking the
    event loop between awaits.
    """
    pending: dict[asyncio.Task[TestResult], str] = {}
    results: dict[str, TestResult] = {}
    started = time.monotonic()

    for tester in testers:
        given = credentials.get(tester.service_key)
        if given is None:
            results[tester.service_key] = TestResult(
                service_key=tester.service_key,
                outcome="not_configured",
                checks=(
                    CheckResult(
                        name="configured",
                        passed=False,
                        detail="no stored settings and none submitted for this service",
                        remedy=("enter this service's endpoint and credentials, then test again"),
                        elapsed_ms=0,
                    ),
                ),
            )
            continue
        task = asyncio.ensure_future(_run_one(tester, given))
        pending[task] = tester.service_key

    if pending:
        done, still_running = await asyncio.wait(set(pending), timeout=whole_call_budget)
        for task in done:
            results[pending[task]] = task.result()
        for task in still_running:
            task.cancel()
        if still_running:
            # Await the cancellations so no task outlives the request, and
            # take the timeout result each one produces on the way down.
            for task in still_running:
                try:
                    results[pending[task]] = await task
                except asyncio.CancelledError:  # pragma: no cover - defensive
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    results[pending[task]] = _timed_out(
                        pending[task], whole_call_budget, elapsed_ms
                    )

    return [results[tester.service_key] for tester in testers]


def _stored_name(
    stored: DeploymentService | None, model: type[ServiceSettings], field_name: str
) -> str | None:
    """The SecretStore name a stored row uses for one secret field. The broker
    password lives in its own column and everything else in the map - E5.1's
    split, read in one place so no tester has to know about it."""
    if stored is None:
        return None
    if field_name == model.secret_column_field:
        return stored.password_secret_name
    return stored.secret_names.get(field_name)


def _fetch(secret_getter: Callable[[str], str], service_key: str, stored_name: str) -> str | None:
    """A named secret, or None with a warning naming the SECRET NAME and never
    its value. A row naming a secret the store has lost is a real state (a
    half-rolled-back save), and it must not become a 500 that hides the other
    four verdicts."""
    try:
        return secret_getter(stored_name)
    except Exception:  # noqa: BLE001  (any store failure is the tester's problem, not a 500)
        logger.warning(
            "service %s: secret %s named on the row could not be read",
            service_key,
            stored_name,
        )
        return None


def resolve_credentials(
    service_key: str,
    stored: DeploymentService | None,
    candidate: ServiceSettings | None,
    secret_getter: Callable[[str], str],
    *,
    deployment_id: uuid.UUID | None = None,
    deployment_slug: str | None = None,
) -> ServiceCredentials | None:
    """What to dial one service with, or None if there is nothing to dial.

    **Candidate beats stored, and the keep sentinel is what reaches back for a
    stored value** - the same three-way rule the PUT boundary uses (E5.2,
    D51), so "test" and "save" can never disagree about which credential they
    mean. Testing a candidate that was never saved is exactly spec 16.2's
    "validates each entry with a live connection test before accepting it".

    Raises nothing. A secret named on the row but unreadable yields a
    credential set without that field, and the tester fails on the resulting
    auth error with a real remedy - which is a better answer than a 500 that
    tells the operator nothing about the other four services.

    `deployment_id` and `deployment_slug` ride through untouched for the one
    tester that needs them (E5.4a; see `ServiceCredentials`). They are
    keyword-only and defaulted so the four testers that do not care never
    mention them.
    """
    model = SERVICE_SCHEMAS[service_key]
    plain_fields = [name for name in model.model_fields if name not in model.secret_fields]
    settings: dict[str, Any] = {}
    secrets: dict[str, str] = {}

    if candidate is None:
        if stored is None:
            return None
        for name in plain_fields:
            settings[name] = (
                getattr(stored, name) if name in model.column_fields else stored.config.get(name)
            )
        for name in model.secret_fields:
            stored_name = _stored_name(stored, model, name)
            if stored_name is None:
                continue
            value = _fetch(secret_getter, service_key, stored_name)
            if value is not None:
                secrets[name] = value
        return ServiceCredentials(
            service_key=service_key,
            settings=settings,
            secrets=secrets,
            deployment_id=deployment_id,
            deployment_slug=deployment_slug,
        )

    for name in plain_fields:
        settings[name] = getattr(candidate, name)
    for name in model.secret_fields:
        submitted = getattr(candidate, name)
        if isinstance(submitted, KeepSecret):
            stored_name = _stored_name(stored, model, name)
            if stored_name is None:
                continue
            value = _fetch(secret_getter, service_key, stored_name)
            if value is not None:
                secrets[name] = value
        elif submitted is not None:
            secrets[name] = submitted
    return ServiceCredentials(
        service_key=service_key,
        settings=settings,
        secrets=secrets,
        deployment_id=deployment_id,
        deployment_slug=deployment_slug,
    )
