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
