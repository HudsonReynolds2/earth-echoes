"""The spec 16.5 services status lifecycle (task E5.5).

Two vocabularies, and keeping them apart is most of this module's job:

* **per-service** `untested` / `verified` / `failed`, on `deployment_service`,
  a property of one connection (spec 16.2);
* **rolled up** `unconfigured` / `pending_verification` / `verified` /
  `degraded`, on `deployment.services_status`, a property of the deployment
  (spec 16.5).

They are not aliases and neither is derivable from the other one row at a time
- "this deployment is degraded" is not a statement any single service can make.
E5.3's `TesterOutcome` is a **third** vocabulary (D111), because "I did not
run" and "I ran and it failed" are different facts; `apply_test_results` is the
only place all three meet.

**`roll_up` is the ONLY writer of `deployment.services_status`** (phase-5 fixed
choice 2). The column is denormalized because E6.4's map rollup and E7.4's
Owner fan-out both read it once per deployment inside fan-outs that are already
cross-deployment. The correctness risk that buys is answered by making one pure
function its only source and asserting the invariant across the whole suite -
`test_services_status.py` walks every deployment after every mutation path -
rather than by arguing that the writers will stay in sync.

## Why `consecutive_failures` is state and not a heuristic

Spec 16.5 says re-checks demote "on repeated failure". A per-service counter
incremented on failure and zeroed on success is the smallest thing that makes
"repeated" true; a time window would need a history table to answer the same
question. **The threshold guards a demotion, not a first verdict.** An operator
onboarding a service and getting it wrong must see `failed` immediately - that
is the whole point of the wizard - so the counter only protects a service that
had already reached `verified`. A deployment's dot on the map going red because
one re-check hit a restarting container is the failure this exists to prevent,
and it is a worse failure than being one cycle late to notice a real outage.
"""

import logging
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import SERVICE_KEYS, Deployment, DeploymentService
from app.services.testers.base import TestResult

logger = logging.getLogger(__name__)

#: Consecutive failed re-checks before a **verified** service is demoted.
#: Two, not one: spec 16.5's word is "repeated", and one transient failure
#: against a restarting container must not turn a deployment red.
DEGRADE_AFTER_FAILURES = 2

#: Required by spec 16.2 unconditionally. Object storage is deliberately
#: absent: 16.2 makes it CONDITIONALLY required (raw-audio upload can be off),
#: so it counts toward the rollup only when the operator has configured it or
#: when a caller says otherwise - see `roll_up`.
ALWAYS_REQUIRED: tuple[str, ...] = ("mqtt", "influx", "prometheus", "grafana")

if not set(ALWAYS_REQUIRED) <= set(SERVICE_KEYS):  # pragma: no cover - import-time invariant
    raise RuntimeError("ALWAYS_REQUIRED names a service that is not in models.SERVICE_KEYS")


def required_keys(rows: Sequence[DeploymentService]) -> frozenset[str]:
    """Which services this deployment has to get to `verified`.

    The four in `ALWAYS_REQUIRED` unconditionally, plus any other service whose
    row says it is required. `deployment_service.required` defaults to true, so
    a conditionally-required service the operator has actually configured
    counts - having entered object-storage credentials they presumably mean to
    use them, and a bucket that rejects the platform is worth a red dot - until
    E5.4e's tester answers `not_required` and `apply_test_results` records it.

    **The flag is read from the row and never passed in** (D117). Spec 16.2's
    conditional requirement is discovered during a test run, but the rollup is
    recomputed on paths that have no test results in hand - a save, or the
    suite-wide invariant walking every deployment. A required-set that only a
    live test could reconstruct would make `deployment.services_status`
    irreproducible from the rows, which is precisely the drift fixed choice 2
    exists to rule out.

    The four are taken from `ALWAYS_REQUIRED` rather than from their own rows
    on purpose: they are unconditional in spec 16.2, so no verdict and no
    stray write should be able to excuse one. Only object storage is ever
    optional, and it is optional by being absent or by being flagged.
    """
    by_key = {row.service_key: row for row in rows}
    return frozenset(set(ALWAYS_REQUIRED) | {key for key, row in by_key.items() if row.required})


def roll_up(rows: Sequence[DeploymentService]) -> str:
    """One deployment's `services_status`, from its service rows.

    Pure, and the only writer's only source. The order of the branches is the
    substance:

    1. **`unconfigured`** - nothing is configured at all. A deployment nobody
       has started onboarding, not one that is failing.
    2. **`degraded`** - some required service is `failed`. Louder than a
       missing one, so it wins over `pending_verification` even when another
       required service has no row: an operator with one broken service and
       one unentered service needs to see the broken one.
    3. **`verified`** - every required service is configured AND `verified`.
       The only state spec 16.5 lets a provisioning bundle be generated from.
    4. **`pending_verification`** - otherwise: something is configured, none
       of it is failing, and it is not all verified yet.

    A service that is configured but NOT required (object storage on a
    deployment with raw-audio upload off) is ignored entirely - it cannot make
    the deployment red and cannot hold it out of `verified`.
    """
    by_key = {row.service_key: row for row in rows}
    required = required_keys(rows)

    if not by_key.keys() & required:
        return "unconfigured"
    if any(by_key[key].status == "failed" for key in required if key in by_key):
        return "degraded"
    if all(key in by_key and by_key[key].status == "verified" for key in required):
        return "verified"
    return "pending_verification"


def recompute(db: Session, deployment_id: uuid.UUID) -> str:
    """Recompute and store one deployment's rollup. Does not commit.

    The single write path. Every mutation that can change a service row calls
    this, and `test_services_status.py` asserts across the suite that no
    deployment's stored value ever differs from `roll_up` over its own rows -
    which is the whole justification for denormalizing the column.
    """
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        # A services mutation for a deployment that no longer exists is a
        # race with its DELETE, not an error worth a 500 - the rows are gone
        # either way.
        return "unconfigured"
    rows = list(
        db.scalars(
            select(DeploymentService).where(DeploymentService.deployment_id == deployment_id)
        )
    )
    deployment.services_status = roll_up(rows)
    return deployment.services_status


@dataclass(frozen=True)
class AppliedResult:
    """What one service's row became, for the caller's audit detail."""

    service_key: str
    status: str
    consecutive_failures: int
    #: True when a failure was absorbed rather than demoting the service -
    #: the operator-visible difference between "flaky" and "down".
    tolerated: bool = False


def _redacted_detail(result: TestResult) -> dict[str, object]:
    """The structured result as it is stored on the row.

    Every string here came from a tester, and a tester's `detail` and `remedy`
    are already forbidden from naming a credential (E5.3). Stored as-is rather
    than re-serialized through a second model, because a second shape is a
    second place for that rule to be forgotten.
    """
    return {
        "outcome": result.outcome,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "detail": check.detail,
                "remedy": check.remedy,
                "elapsed_ms": check.elapsed_ms,
            }
            for check in result.checks
        ],
    }


def _first_failure_reason(result: TestResult) -> str | None:
    for check in result.checks:
        if not check.passed:
            return check.detail
    return None


def apply_test_results(
    db: Session,
    deployment_id: uuid.UUID,
    results: Iterable[TestResult],
    *,
    now: datetime | None = None,
) -> list[AppliedResult]:
    """Write one test run's verdicts onto the rows, then recompute the rollup.

    Does not commit - the caller owns the transaction, because a services save
    that tests and stores in one request must not half-commit.

    The three tester outcomes map like this, and none of it is arbitrary:

    * **`pass`** -> `verified`, counter zeroed. One success clears a history of
      failures; a service that is answering is answering.
    * **`fail`** -> counter incremented. Demoted to `failed` **unless** the row
      was already `verified` and the counter has not yet reached
      `DEGRADE_AFTER_FAILURES`, in which case the failure is recorded (reason,
      detail, timestamp, counter) and the status is left alone. See the module
      docstring for why the threshold guards only a demotion.
    * **`not_required`** -> no status is written, because it is not a verdict
      about a connection: it says the service is not needed here (spec 16.2's
      conditional requirement). The one thing it DOES write is
      `row.required = False`, so a deployment with object storage switched off
      can reach `verified` - and can still be recomputed to `verified` later by
      a save or a sweep that never ran a test (D117).
    * **`not_configured`** -> the row is not touched at all. There was nothing
      to dial. Writing `failed` for either of these is what teaches an operator
      to ignore red.

    A real verdict (`pass` or `fail`) sets `row.required = True`: the tester
    reached a decision about a live connection, so the service is in use. That
    is what flips object storage back to required when an operator turns
    raw-audio upload on again.
    """
    stamp = now or datetime.now(UTC)
    rows = {
        row.service_key: row
        for row in db.scalars(
            select(DeploymentService).where(DeploymentService.deployment_id == deployment_id)
        )
    }
    applied: list[AppliedResult] = []

    for result in results:
        if result.outcome == "not_configured":
            continue
        row = rows.get(result.service_key)
        if row is None:
            # A candidate-credentials test against a service with no row yet
            # (spec 16.2 tests "before accepting"). There is nothing to write
            # a status onto, and inventing a row here would make a test a
            # write - which the endpoint's own docstring promises it is not.
            continue
        if result.outcome == "not_required":
            # The only thing this writes. Persisted rather than returned so
            # every later recompute reaches the same answer without a test
            # (D117, and this migration's docstring).
            row.required = False
            continue

        row.required = True
        row.last_tested_at = stamp
        row.last_test_detail = _redacted_detail(result)

        if result.outcome == "pass":
            row.status = "verified"
            row.status_reason = None
            row.consecutive_failures = 0
            applied.append(AppliedResult(result.service_key, row.status, 0))
            continue

        row.consecutive_failures += 1
        row.status_reason = _first_failure_reason(result)
        tolerated = row.status == "verified" and row.consecutive_failures < DEGRADE_AFTER_FAILURES
        if not tolerated:
            row.status = "failed"
        else:
            logger.info(
                "service %s for deployment %s failed a re-check (%d of %d) and stays verified",
                result.service_key,
                deployment_id,
                row.consecutive_failures,
                DEGRADE_AFTER_FAILURES,
            )
        applied.append(
            AppliedResult(result.service_key, row.status, row.consecutive_failures, tolerated)
        )

    db.flush()
    recompute(db, deployment_id)
    return applied


def audited_outcomes(applied: Sequence[AppliedResult]) -> dict[str, object]:
    """The audit detail for a status write: keys and verdicts, never a reason.

    A `status_reason` is operator-facing prose composed by a tester, and while
    it may not name a credential it may well name a host - the audit log is
    not where that earns its keep.
    """
    return {
        result.service_key: {
            "status": result.status,
            "consecutive_failures": result.consecutive_failures,
            "tolerated": result.tolerated,
        }
        for result in applied
    }


# --- The periodic re-check sweep --------------------------------------------
#
# Spec 16.5: "Periodic re-checks, reusing the Section 10 read clients and
# lightweight probes, demote a deployment to `degraded` on repeated failure."
#
# **This module ships the sweep as a callable and does NOT register it on the
# reconciliation worker.** `ReconciliationWorker`'s sweep runner lives in
# `app/controlplane/runner.py`, which is E3-owned, and the phase document
# authorizes exactly two discretionary E3-owned edits, both in E5.7b - a third
# is a stop-and-ask (rule R2). E5.7b is already opening that file to add
# `service_config_sweep`, so it registers this one in the same edit and the
# E3-owned surface this epic changes stays exactly what the document says it
# is. The sweep is fully testable here by calling it, which is how
# `test_services_status.py` exercises it.


@runtime_checkable
class ServiceTestRunner(Protocol):
    """What the sweep needs: run one deployment's testers and hand back the
    results. A protocol rather than a concrete import, so the sweep can be
    tested without a container and so E7's "lightweight probes" (spec 16.5)
    can be substituted for full connection tests without touching it."""

    async def __call__(self, db: Session, deployment_id: uuid.UUID) -> Sequence[TestResult]: ...


async def recheck_deployment(
    db: Session,
    deployment_id: uuid.UUID,
    runner: ServiceTestRunner,
) -> list[AppliedResult]:
    """Re-test one deployment's configured services and write the verdicts."""
    results = await runner(db, deployment_id)
    return apply_test_results(db, deployment_id, results)


async def services_recheck_sweep(
    session_factory: sessionmaker[Session],
    runner: ServiceTestRunner,
    *,
    only: Collection[uuid.UUID] | None = None,
) -> Mapping[uuid.UUID, list[AppliedResult]]:
    """Re-check every deployment that has any service configured.

    **One transaction per deployment**, following `runner.py`'s own rule for
    its sweeps: a sweep that failed halfway must leave the deployments it
    already handled written, and one that committed once at the end would hold
    locks across every deployment's network round trips.

    A deployment with no service rows is skipped rather than written to
    `unconfigured` - it already is, and touching every untouched deployment on
    every cycle is how a sweep becomes the reason a database is busy.
    """
    outcomes: dict[uuid.UUID, list[AppliedResult]] = {}
    with session_factory() as session:
        deployment_ids = list(
            session.scalars(
                select(DeploymentService.deployment_id)
                .distinct()
                .order_by(DeploymentService.deployment_id)
            )
        )
    if only is not None:
        wanted = set(only)
        deployment_ids = [one for one in deployment_ids if one in wanted]

    for deployment_id in deployment_ids:
        try:
            with session_factory() as session:
                outcomes[deployment_id] = await recheck_deployment(session, deployment_id, runner)
                session.commit()
        except Exception:  # noqa: BLE001  (one deployment must not stop the sweep)
            logger.exception("services re-check failed for deployment %s", deployment_id)
    return outcomes
