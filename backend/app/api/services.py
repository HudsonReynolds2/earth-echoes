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

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, require_permission
from app.config.plan import PlanError, apply_change_plan, build_change_plan
from app.config.selection import MatchedEntity
from app.controlplane.publisher import publish_all
from app.errors import AppError
from app.models import SERVICE_KEYS, ConfigRevision, Deployment, DeploymentService, UserSession
from app.secrets import SecretStore
from app.services import bundle, stackgen
from app.services import testers as testers_module
from app.services.projection import service_settings
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
from app.services.status import (
    DEGRADE_AFTER_FAILURES,
    apply_test_results,
    audited_outcomes,
    recompute,
    required_keys,
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


class ServiceStatusOut(BaseModel):
    service_key: str
    configured: bool
    #: Whether this service has to reach `verified` for the deployment to.
    #: Object storage is conditionally required (spec 16.2), so this is not a
    #: constant the frontend can hardcode.
    required: bool
    status: str
    status_reason: str | None
    last_tested_at: datetime | None
    consecutive_failures: int


class ServicesStatusOut(BaseModel):
    """The spec 16.5 status endpoint (E5.5).

    Two vocabularies in one body, deliberately: `services_status` is the
    deployment's rollup and `services[key].status` is each connection's own
    verdict. They are not aliases, and a UI that rendered one as the other
    would tell an operator their whole deployment is broken because one
    optional service is.
    """

    deployment_id: str
    services_status: str
    #: How many consecutive failures demote a verified service, so the wizard
    #: can say "1 of 2 failed checks" rather than hardcoding the platform's
    #: threshold.
    degrade_after_failures: int
    services: dict[str, ServiceStatusOut]


def _deployment_status(db: DbDep, deployment_id: uuid.UUID) -> str:
    """The stored rollup, read back after a write in the same transaction."""
    deployment = db.get(Deployment, deployment_id)
    return deployment.services_status if deployment is not None else "unconfigured"


def _status_out(db: DbDep, deployment_id: uuid.UUID) -> ServicesStatusOut:
    rows = {row.service_key: row for row in load_services(db, deployment_id)}
    required = required_keys(list(rows.values()))
    return ServicesStatusOut(
        deployment_id=str(deployment_id),
        services_status=_deployment_status(db, deployment_id),
        degrade_after_failures=DEGRADE_AFTER_FAILURES,
        services={
            key: ServiceStatusOut(
                service_key=key,
                configured=key in rows,
                required=key in required,
                status=rows[key].status if key in rows else "untested",
                status_reason=rows[key].status_reason if key in rows else None,
                last_tested_at=rows[key].last_tested_at if key in rows else None,
                consecutive_failures=rows[key].consecutive_failures if key in rows else 0,
            )
            for key in SERVICE_KEYS
        },
    )


@router.get("/status", response_model=ServicesStatusOut)
def get_services_status(
    deployment_id: uuid.UUID,
    db: DbDep,
    _: Annotated[
        UserSession, Depends(require_permission(Permission.VIEW_SERVICES, "deployment_id"))
    ],
) -> ServicesStatusOut:
    """The spec 16.5 rollup and its per-service evidence.

    **`VIEW_SERVICES`, not `MANAGE_SERVICES`** - status has to render for all
    four roles (fixed choice 9), and nothing here is a credential. The
    `status_reason` is a tester's operator-facing sentence, which E5.3 already
    forbids from naming one.

    Reads; it does not recompute. `roll_up` runs on the mutation paths that can
    change the answer, and a GET that wrote would make a read a write and put
    two writers on a column whose whole design (fixed choice 2) is that it has
    exactly one.
    """
    _get_deployment(db, deployment_id)
    return _status_out(db, deployment_id)


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
async def put_services(
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
        row = upsert_service(
            db,
            deployment_id,
            plan.service_key,
            config=plan.config,
            secret_names=plan.secret_names,
            password_secret_name=plan.password_secret_name,
            **plan.columns,
        )
        # E5.5: saving a service's settings UNVERIFIES it. A verdict is about
        # the credentials that produced it, and the operator has just replaced
        # them - spec 16.2 verifies each entry with a live test, so carrying an
        # old `verified` across a new endpoint would let a deployment stay
        # green against a service it can no longer reach. The counter goes with
        # it: the next failure is this credential's first, not the last one's
        # third.
        row.status = "untested"
        row.status_reason = None
        row.consecutive_failures = 0
        row.last_test_detail = None
        row.last_tested_at = None

    revisions: list[ConfigRevision] = []
    if plans:
        db.flush()
        recompute(db, deployment_id)
        revisions = _project_to_config(db, store, deployment_id, actor)
        record_audit(
            db,
            action="services.update",
            entity_type="deployment",
            entity_id=str(deployment_id),
            actor_user_id=actor.user_id,
            scope=deployment_id,
            # Field NAMES only. `audited_fields` cannot reach a value.
            detail={
                "services": audited_fields(plans),
                # E5.7a: how many devices this save actually told. Expected to
                # be one per Aggregator and ZERO per Listener — spec 5.4 keeps
                # these keys off Listener-bound config, so their snapshots do
                # not change and `no_op` is true for every one of them.
                "revisions": len(revisions),
            },
        )
    db.commit()

    for plan in plans.values():
        for name in plan.secrets_to_delete:  # D51: only ever AFTER the commit
            store.delete(name)

    # AFTER the commit, for E3.13's reason: the operator's save is durable the
    # moment it lands, and a broker that is down costs them a publish rather
    # than their work. Each revision that could not go out stays `draft` and
    # `POST /revisions/{id}/publish` retries it.
    await publish_all(
        request.app.state.session_factory,
        getattr(request.app.state, "mqtt", None),
        revisions,
        publish_enabled=request.app.state.settings.publish_enabled,
        actor_user_id=actor.user_id,
    )
    return _services_out(db, deployment_id)


def _project_to_config(
    db: DbDep,
    store: SecretStore,
    deployment_id: uuid.UUID,
    actor: UserSession,
) -> list[ConfigRevision]:
    """Regenerate the twelve device-facing keys and mint the revisions.

    **Spec 16.4's post-connect delivery, expressed as ordinary config.** The
    service rows are the source of truth and the deployment's `entity_override`
    row is a derived cache of them (phase-5 fixed choice 3), so every save
    replaces the projection wholesale — `allow_write_restricted=True` is what
    both permits these keys and makes the write a regeneration rather than a
    merge, so a field the operator cleared disappears instead of surviving.

    **It runs through `build_change_plan` / `apply_change_plan` rather than
    writing revisions itself**, so a services save and a bulk config edit reach
    devices through one code path with one set of rules about no-ops, secret
    markers and checksums. That is also what makes the acceptance measurable:
    one revision per Aggregator, zero per Listener.

    Staged in the caller's transaction and NOT committed here — the rows, the
    projection, the revisions and the audit entry are one unit or none of them.
    """
    rows = load_services(db, deployment_id)
    projection = service_settings(rows, store.get)
    matched = [
        MatchedEntity(
            entity_type="deployment",
            entity_id=str(deployment_id),
            name="",
            deployment_id=str(deployment_id),
            tags=(),
        )
    ]
    try:
        plan = build_change_plan(
            db,
            matched,
            projection,
            "deployment",
            actor.user.role_assignments,
            allow_write_restricted=True,
        )
        revisions, _ = apply_change_plan(
            db, store, plan, projection, actor.user_id, allow_write_restricted=True
        )
    except PlanError as error:
        # A projection that the catalog refuses is a platform defect, not an
        # operator mistake: every key comes from `PROJECTION`, which is asserted
        # against `CATALOG` at import. Surfaced rather than swallowed, with the
        # per-key detail, because silently saving the services and not
        # delivering them is the one outcome nobody could diagnose.
        raise AppError(
            "validation_error",
            "the deployment's service settings could not be projected onto device config",
            status_code=422,
            detail=error.detail,
        ) from error
    return revisions


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
    #: The spec 16.5 rollup AFTER this run's verdicts of record were written
    #: (E5.5), so the wizard's Verify step does not need a second request to
    #: learn whether it may now generate a bundle.
    services_status: str
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

    # E5.5: a verdict about STORED credentials becomes the row's status; a
    # verdict about candidate credentials does not. The difference matters and
    # is not fussiness - a wizard testing what the operator has typed but not
    # yet saved must not leave the deployment recorded as `verified` against a
    # credential the platform is not holding. Spec 16.2's "validates each entry
    # before accepting it" is precisely a test that has not been accepted yet.
    of_record = [result for result in results if result.service_key not in submitted]
    applied = apply_test_results(db, deployment_id, of_record)

    record_audit(
        db,
        action="services.test",
        entity_type="deployment",
        entity_id=str(deployment_id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        # Outcomes by service key. No check detail and no candidate value: a
        # remedy string is operator-facing text, and the audit log is not
        # where it earns its keep. `recorded` names only the services whose
        # verdict was written, so the log distinguishes a rehearsal from a
        # test of record.
        detail={
            "outcomes": {result.service_key: result.outcome for result in results},
            "recorded": audited_outcomes(applied),
        },
    )
    db.commit()

    return ServicesTestOut(
        deployment_id=str(deployment_id),
        services_status=_deployment_status(db, deployment_id),
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


# --- E5.10: the generated stack bundle --------------------------------------
#
# Two endpoints and no storage between them. `POST` generates every credential
# and commits (E5.9); `GET .../download` re-renders from those committed rows
# and streams. Fixed choice 7: no blob column, no temp directory, no cleanup
# job, and no window in which a bundle exists whose credentials the platform
# cannot verify.


class StackGenerateBody(BaseModel):
    """What the operator chooses about the stack. Everything else is derived."""

    model_config = ConfigDict(extra="forbid")

    #: The address Aggregators and the platform will dial this stack at. Reaches
    #: the broker certificate's SAN, so it has to be right at generation time —
    #: a certificate for the wrong name fails verification everywhere.
    hostname: str = Field(default="localhost", min_length=1, max_length=255)
    ip: str | None = Field(default=None, max_length=45)
    #: D123: object storage is conditionally required. An operator not
    #: uploading raw audio leaves it off and the deployment can still verify.
    include_object_storage: bool = False


class StackOut(BaseModel):
    deployment_id: str
    services_status: str
    services: list[str]
    #: NOT a credential and not a URL with one in it. The archive is fetched
    #: through the authenticated download endpoint like anything else.
    download_path: str


def _stack_out(db: DbDep, deployment_id: uuid.UUID) -> StackOut:
    return StackOut(
        deployment_id=str(deployment_id),
        services_status=_deployment_status(db, deployment_id),
        services=[row.service_key for row in load_services(db, deployment_id)],
        download_path=f"/deployments/{deployment_id}/services/stack/download",
    )


@router.post("/stack", response_model=StackOut, dependencies=[Depends(require_csrf)])
def generate_stack_bundle(
    deployment_id: uuid.UUID,
    db: DbDep,
    request: Request,
    body: StackGenerateBody,
    actor: Annotated[
        UserSession, Depends(require_permission(Permission.MANAGE_SERVICES, "deployment_id"))
    ],
) -> StackOut:
    """Generate the deployment's stack: credentials, rows, TLS material.

    **`MANAGE_SERVICES`.** This mints the deployment's keys to everything, and
    fixed choice 9 keeps that away from a Field Tech whose job is hardware.

    Every service lands `untested`, which by spec 16.5 leaves the deployment
    short of `verified` until the operator actually runs the stack and tests
    it. A generated stack does not get to vouch for itself.
    """
    deployment = _get_deployment(db, deployment_id)
    secret_store = request.app.state.secret_store

    stackgen.generate_stack(
        db,
        secret_store,
        deployment,
        include_object_storage=body.include_object_storage,
        hostnames=(body.hostname,),
        ips=(body.ip,) if body.ip else (),
    )
    recompute(db, deployment_id)
    record_audit(
        db,
        action="services.stack.generate",
        entity_type="deployment",
        entity_id=str(deployment_id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        # Choices and counts. Not one generated credential, not the hostname's
        # certificate, nothing that would make the audit log a second copy of
        # the bundle (rule R2).
        detail={
            "include_object_storage": body.include_object_storage,
            "services": [row.service_key for row in load_services(db, deployment_id)],
        },
    )
    db.commit()
    return _stack_out(db, deployment_id)


@router.get("/stack/download")
def download_stack_bundle(
    deployment_id: uuid.UUID,
    db: DbDep,
    request: Request,
    actor: Annotated[
        UserSession, Depends(require_permission(Permission.MANAGE_SERVICES, "deployment_id"))
    ],
) -> Response:
    """Stream the bundle, re-rendered from the stored rows.

    **`MANAGE_SERVICES`, not `VIEW_SERVICES`.** Status is for everyone
    (fixed choice 9); this archive contains every credential the deployment
    has, in usable form, and the README says so in its own section.

    Nothing is written to disk server-side. The archive is built in memory and
    handed to the response, so there is no path on this host that ever holds a
    deployment's credentials in the clear.
    """
    deployment = _get_deployment(db, deployment_id)
    secret_store = request.app.state.secret_store
    try:
        generated = stackgen.load_generated_stack(db, secret_store, deployment)
        tls = stackgen.tls_material(secret_store, deployment_id)
    except stackgen.StackNotGenerated as error:
        raise AppError(
            "not_found",
            "no generated stack for this deployment",
            status_code=404,
        ) from error

    archive = bundle.build_archive(bundle.bundle_files(generated, tls))
    record_audit(
        db,
        action="services.stack.download",
        entity_type="deployment",
        entity_id=str(deployment_id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        # Size and nothing else. Who took the deployment's credentials and when
        # is exactly what this record is for; what was in them is not.
        detail={"bytes": len(archive)},
    )
    db.commit()
    return Response(
        content=archive,
        media_type="application/gzip",
        headers={
            "Content-Disposition": (f'attachment; filename="echoes-stack-{deployment.slug}.tar.gz"')
        },
    )
