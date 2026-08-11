"""Write-only deployment services credentials API (task E5.2; spec 16.2, 13).

Two endpoints over the five `deployment_service` rows E5.1 shaped:

* **`GET /deployments/{id}/services`** - all five services always, configured
  or not, in the spec 16.2 table's order, so the S5 wizard renders a fixed
  set of cards rather than discovering which exist. **Redacted by
  construction**: the response is built from `redacted_settings`, which reads
  the row and never SecretStore, so no branch of this endpoint can return a
  credential.
* **`PUT /deployments/{id}/services`** - one save for any subset of the five.
  A service present in the body is written WHOLESALE (the E1.7 tags
  precedent, and `upsert_service`'s contract): every field the caller omits
  is cleared. A service ABSENT from the body is left completely alone, which
  is what makes a per-service wizard step possible without the frontend
  having to resubmit the other four - and it is also why this endpoint has no
  way to delete a service row. Deleting the `mqtt` row would strand the
  deployment's control plane; the deployment's own DELETE is what removes
  them all (E5.1).

**Permissions** are phase-5 fixed choice 9. `MANAGE_SERVICES` writes and goes
to Owner and Deployment Operator only - service credentials are the
deployment's keys to everything, and a Field Tech provisions hardware.
`VIEW_SERVICES` reads and goes to all four roles, because status renders
everywhere and the read carries no secrets to withhold.

**Secret ordering is D51's**, and split across the commit deliberately:
`SecretStore.put` runs before it (a ciphertext no row points at is
unreachable and harmless), `SecretStore.delete` only after (deleting first
loses the plaintext if the transaction rolls back).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, require_permission
from app.errors import AppError
from app.models import SERVICE_KEYS, Deployment, DeploymentService, UserSession
from app.services import testers as testers_module
from app.services.schemas import (
    GrafanaSettings,
    InfluxSettings,
    MqttSettings,
    PrometheusSettings,
    S3Settings,
    ServiceSettings,
    ServiceSettingsError,
    ServiceWritePlan,
    audited_fields,
    plan_write,
    redacted_settings,
)
from app.services.store import load_service, load_services, upsert_service
from app.services.testers import resolve_credentials, run_testers

router = APIRouter(prefix="/deployments/{deployment_id}/services")


class ServiceOut(BaseModel):
    """One service's public state. `settings` is redacted (set secrets render
    as the D51 keep sentinel) and is `dict[str, Any]` on purpose: its shape is
    per-service and already typed on the way IN by the five models in
    `app.services.schemas`, and a five-way discriminated union in the response
    schema would buy the wizard nothing it does not already know from the key.
    """

    service_key: str
    configured: bool
    status: str
    status_reason: str | None
    last_tested_at: datetime | None
    consecutive_failures: int
    settings: dict[str, Any]


class ServicesOut(BaseModel):
    deployment_id: str
    #: All five, keyed by service_key, inserted in the spec 16.2 order so a
    #: client rendering `Object.entries()` gets the spec's order for free.
    services: dict[str, ServiceOut]


class ServicesIn(BaseModel):
    """The submitted subset. Five explicitly named optional fields rather
    than a `dict[str, Any]`, so FastAPI itself rejects a sixth service key and
    locates every field error - the typing rule R2 asks for, at the one
    boundary where it decides whether a credential is stored."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttSettings | None = None
    influx: InfluxSettings | None = None
    prometheus: PrometheusSettings | None = None
    grafana: GrafanaSettings | None = None
    s3: S3Settings | None = None

    def submitted(self) -> list[ServiceSettings]:
        """The present services in spec 16.2 order. Absent and explicit null
        are the same thing here - untouched - because there is no delete."""
        candidates: tuple[ServiceSettings | None, ...] = (
            self.mqtt,
            self.influx,
            self.prometheus,
            self.grafana,
            self.s3,
        )
        return [settings for settings in candidates if settings is not None]

    def by_key(self) -> dict[str, ServiceSettings]:
        """The same set, keyed by `service_key`, for the E5.3 test runner."""
        return {type(settings).service_key: settings for settings in self.submitted()}


class ServicesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    services: ServicesIn


def _get_deployment(db: DbDep, deployment_id: uuid.UUID) -> Deployment:
    """404 after the permission check, which `require_permission(...,
    "deployment_id")` has already run - so this never confirms existence to a
    caller who is not allowed to know (the E1.2 item-route pattern)."""
    row = db.get(Deployment, deployment_id)
    if row is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    return row


def _services_out(db: DbDep, deployment_id: uuid.UUID) -> ServicesOut:
    rows = {row.service_key: row for row in load_services(db, deployment_id)}
    services: dict[str, ServiceOut] = {}
    for key in SERVICE_KEYS:
        row: DeploymentService | None = rows.get(key)
        services[key] = (
            ServiceOut(
                service_key=key,
                configured=True,
                status=row.status,
                status_reason=row.status_reason,
                last_tested_at=row.last_tested_at,
                consecutive_failures=row.consecutive_failures,
                settings=redacted_settings(row),
            )
            if row is not None
            else ServiceOut(
                service_key=key,
                configured=False,
                status="untested",
                status_reason=None,
                last_tested_at=None,
                consecutive_failures=0,
                settings={},
            )
        )
    return ServicesOut(deployment_id=str(deployment_id), services=services)


@router.get("", response_model=ServicesOut)
def get_services(
    deployment_id: uuid.UUID,
    db: DbDep,
    _: Annotated[
        UserSession, Depends(require_permission(Permission.VIEW_SERVICES, "deployment_id"))
    ],
) -> ServicesOut:
    _get_deployment(db, deployment_id)
    return _services_out(db, deployment_id)


@router.put("", response_model=ServicesOut, dependencies=[Depends(require_csrf)])
def put_services(
    deployment_id: uuid.UUID,
    db: DbDep,
    request: Request,
    body: ServicesBody,
    actor: Annotated[
        UserSession, Depends(require_permission(Permission.MANAGE_SERVICES, "deployment_id"))
    ],
) -> ServicesOut:
    _get_deployment(db, deployment_id)
    store = request.app.state.secret_store

    plans: dict[str, ServiceWritePlan] = {}
    for settings in body.services.submitted():
        key = type(settings).service_key
        try:
            plans[key] = plan_write(settings, deployment_id, load_service(db, deployment_id, key))
        except ServiceSettingsError as error:
            raise AppError(
                "validation_error",
                "invalid service settings",
                status_code=422,
                detail={
                    "errors": [
                        {
                            "service_key": error.service_key,
                            "field": error.field,
                            "code": error.code,
                            "message": error.message,
                        }
                    ]
                },
            ) from error

    # Every plan is decided before anything is written, so a rejected second
    # service does not leave the first one half-saved.
    for plan in plans.values():
        for name, plaintext in plan.secrets_to_put.items():
            store.put(name, plaintext)
        upsert_service(
            db,
            deployment_id,
            plan.service_key,
            config=plan.config,
            secret_names=plan.secret_names,
            password_secret_name=plan.password_secret_name,
            **plan.columns,
        )

    if plans:
        record_audit(
            db,
            action="services.update",
            entity_type="deployment",
            entity_id=str(deployment_id),
            actor_user_id=actor.user_id,
            scope=deployment_id,
            # Field NAMES only. `audited_fields` cannot reach a value.
            detail={"services": audited_fields(plans)},
        )
    db.commit()

    for plan in plans.values():
        for name in plan.secrets_to_delete:  # D51: only ever AFTER the commit
            store.delete(name)
    return _services_out(db, deployment_id)


# --- E5.3: the connection test endpoint --------------------------------------


class CheckOut(BaseModel):
    name: str
    passed: bool
    detail: str
    #: Non-empty on every failing check, and asserted so across the suite. S5's
    #: premise is that an operator reads a failure and fixes their service.
    remedy: str
    elapsed_ms: int


class TestResultOut(BaseModel):
    service_key: str
    #: pass | fail | not_required | not_configured. The last two are NOT
    #: failures - see `app/services/testers/base.py::TesterOutcome`.
    outcome: str
    checks: list[CheckOut]


class ServicesTestOut(BaseModel):
    deployment_id: str
    results: list[TestResultOut]


class ServicesTestBody(BaseModel):
    """Candidate credentials, or nothing.

    Spec 16.2 says the platform "validates each entry with a live connection
    test **before accepting it**", so the body is the unsaved form: the same
    five typed models the PUT takes, with the keep sentinel reaching back for a
    stored credential. Omit the body entirely to test what is stored.
    """

    model_config = ConfigDict(extra="forbid")

    services: ServicesIn = ServicesIn()


@router.post("/test", response_model=ServicesTestOut, dependencies=[Depends(require_csrf)])
async def test_services(
    deployment_id: uuid.UUID,
    db: DbDep,
    request: Request,
    actor: Annotated[
        UserSession, Depends(require_permission(Permission.MANAGE_SERVICES, "deployment_id"))
    ],
    body: ServicesTestBody | None = None,
) -> ServicesTestOut:
    """Run every registered tester concurrently and report structured results.

    **MANAGE_SERVICES, not VIEW_SERVICES**: the body carries candidate
    credentials, so this is a write-shaped act even though it stores nothing.

    **It stores nothing.** `deployment_service.status` and
    `deployment.services_status` are E5.5's to write from these results; this
    endpoint computes evidence and returns it, so re-running a test can never
    itself change a verdict of record.

    **`testers_module.REGISTRY` fills up across E5.4a-e** (`mqtt` is in it as of E5.4a). A
    service with no tester yet is simply absent from the results rather than carrying a
    verdict nothing computed.
    """
    deployment = _get_deployment(db, deployment_id)
    store = request.app.state.secret_store
    submitted = (body.services if body is not None else ServicesIn()).by_key()

    # Read through the MODULE, not a from-import: E5.4a-e register into
    # `testers_module.REGISTRY` at import time, and a rebound name here
    # would silently keep whichever dict was current when this module loaded.
    registry = testers_module.REGISTRY
    testers = [registry[key] for key in SERVICE_KEYS if key in registry]
    credentials = {
        tester.service_key: resolve_credentials(
            tester.service_key,
            load_service(db, deployment_id, tester.service_key),
            submitted.get(tester.service_key),
            store.get,
            # For the one tester whose target topic is a function of the
            # deployment (E5.4a's reserved leaf under `deployment_root`).
            # The other four dial a URL and ignore both.
            deployment_id=deployment_id,
            deployment_slug=deployment.slug,
        )
        for tester in testers
    }
    results = await run_testers(testers, credentials)

    record_audit(
        db,
        action="services.test",
        entity_type="deployment",
        entity_id=str(deployment_id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        # Outcomes by service key. No check detail and no candidate value: a
        # remedy string is operator-facing text, and the audit log is not
        # where it earns its keep.
        detail={"outcomes": {result.service_key: result.outcome for result in results}},
    )
    db.commit()

    return ServicesTestOut(
        deployment_id=str(deployment_id),
        results=[
            TestResultOut(
                service_key=result.service_key,
                outcome=result.outcome,
                checks=[
                    CheckOut(
                        name=check.name,
                        passed=check.passed,
                        detail=check.detail,
                        remedy=check.remedy,
                        elapsed_ms=check.elapsed_ms,
                    )
                    for check in result.checks
                ],
            )
            for result in results
        ],
    )
