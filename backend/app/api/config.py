"""Config surface (tasks E2.1, E2.6; spec 5.2, 5.3, 13, 14.4; D47, D55-D56).

GET /config/catalog is a schema document, not a D7 list (D47): the frontend
renders its editors from it wholesale, so it ships unpaginated with a
top-level version and items sorted by key for deterministic rendering.

POST /config/preview and POST /config/apply share ONE body shape and ONE
plan builder (app/config/plan.py) - identical inputs through identical code
is what makes "preview matches what apply then produces" structural rather
than aspirational. Preview mutates nothing (no CSRF - the project's rule is
CSRF on mutations); apply stages everything and commits once, stopping at
draft revisions unconditionally (publication is E3's, gated by
EOE_PUBLISH_ENABLED which E2 only reports).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.entity_config import ResolvedValueOut
from app.api.pagination import ListResponse
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission
from app.config.catalog import CATALOG_BY_KEY
from app.config.merge import ResolvedValue, redact_secrets
from app.config.plan import ChangePlan, PlanError, apply_change_plan, build_change_plan
from app.config.selection import SelectionQuery, evaluate_selection, validate_selection_query
from app.errors import AppError
from app.models import Selection, SettingsCatalog, UserSession
from app.scoping import require_any_assignment

router = APIRouter(prefix="/config")


class CatalogItemOut(BaseModel):
    key: str
    value_type: str
    enum_values: list[Any] | None
    min_value: float | None
    max_value: float | None
    default: Any
    lowest_level: str
    secret: bool
    resolution: str
    write_restricted: str | None
    notes: str


class CatalogOut(BaseModel):
    version: int
    items: list[CatalogItemOut]


class SelectionRef(BaseModel):
    model_config = {"extra": "forbid"}

    selection_id: uuid.UUID


class PlanBody(BaseModel):
    """One body for preview AND apply - the parity guarantee (D56)."""

    model_config = {"extra": "forbid"}

    selection: SelectionQuery | SelectionRef
    changes: dict[str, Any] = Field(min_length=1)
    level: Literal["target", "organization", "deployment", "pod", "aggregator"] = "target"


class DevicePlanOut(BaseModel):
    target_type: str
    target_id: str
    name: str
    pod_id: str
    pod_name: str
    deployment_id: str
    changed_keys: list[str]
    no_op: bool
    before: dict[str, ResolvedValueOut]
    after: dict[str, ResolvedValueOut]


class AppliedRevisionOut(BaseModel):
    revision_id: uuid.UUID
    target_type: str
    target_id: str
    deployment_id: uuid.UUID
    changed_keys: list[str]
    checksum: str


class ApplyOut(BaseModel):
    state: str
    publish_enabled: bool
    revisions: list[AppliedRevisionOut]


def _plan_for(db: DbDep, session: UserSession, body: PlanBody) -> ChangePlan:
    if isinstance(body.selection, SelectionRef):
        row = db.get(Selection, body.selection.selection_id)
        if row is None:
            raise AppError("not_found", "selection not found", status_code=404)
        query = SelectionQuery.model_validate(row.query)
    else:
        query = body.selection
    problems = validate_selection_query(query)
    if problems:
        raise AppError(
            "validation_error",
            "invalid selection query",
            status_code=422,
            detail={"errors": problems},
        )
    # MANAGE_CONFIG visibility in BOTH preview and apply - the same actor
    # sees and writes the same set, which is what makes preview==apply hold
    # per actor (D56).
    matched = evaluate_selection(db, query, session.user.role_assignments, Permission.MANAGE_CONFIG)
    try:
        return build_change_plan(
            db, matched, body.changes, body.level, session.user.role_assignments
        )
    except PlanError as error:
        code = "forbidden" if error.status_code == 403 else "validation_error"
        raise AppError(
            code, error.message, status_code=error.status_code, detail=error.detail
        ) from error


def _redacted(raw: dict[str, ResolvedValue]) -> dict[str, ResolvedValueOut]:
    return {
        key: ResolvedValueOut(
            value=rv.value, source=rv.source, source_entity_id=rv.source_entity_id
        )
        for key, rv in redact_secrets(raw, CATALOG_BY_KEY).items()
    }


@router.post("/preview", response_model=ListResponse[DevicePlanOut])
def preview_config_change(
    body: PlanBody,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListResponse[DevicePlanOut]:
    """Per affected device: identity, changed keys, before/after effective
    (redacted). Paginated per spec 14.4 - streaming is deferred to E8.2 as
    a recorded scale seam (D56); v1 fleets are demo-sized."""
    plan = _plan_for(db, session, body)
    window = plan.devices[offset : offset + limit]
    return ListResponse(
        items=[
            DevicePlanOut(
                target_type=device.target_type,
                target_id=device.target_id,
                name=device.name,
                pod_id=device.pod_id,
                pod_name=device.pod_name,
                deployment_id=device.deployment_id,
                changed_keys=list(device.changed_keys),
                no_op=device.no_op,
                before=_redacted(device.before_raw),
                after=_redacted(device.after_raw),
            )
            for device in window
        ],
        total=len(plan.devices),
        limit=limit,
        offset=offset,
    )


@router.post("/apply", response_model=ApplyOut)
def apply_config_change(
    body: PlanBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> ApplyOut:
    """Write the overrides, create draft revisions for every non-no_op
    device, audit once per affected deployment - ONE transaction. Stops at
    draft unconditionally: publication is E3's (EOE_PUBLISH_ENABLED gates
    its eventual call-through; E2 only reports the flag)."""
    plan = _plan_for(db, actor, body)
    revisions, changed_by_deployment = apply_change_plan(
        db, request.app.state.secret_store, plan, body.changes, actor.user_id
    )
    revision_ids_by_deployment: dict[str, list[str]] = {}
    for revision in revisions:
        revision_ids_by_deployment.setdefault(str(revision.deployment_id), []).append(
            str(revision.id)
        )
    for deployment_id, changed_keys in sorted(changed_by_deployment.items()):
        record_audit(
            db,
            action="config.apply",
            entity_type="deployment",
            entity_id=deployment_id,
            actor_user_id=actor.user_id,
            scope=uuid.UUID(deployment_id),
            detail={
                "changed_keys": sorted(changed_keys),
                "revision_ids": revision_ids_by_deployment.get(deployment_id, []),
                "targets": {
                    "aggregators": sum(
                        1
                        for device in plan.devices
                        if device.deployment_id == deployment_id
                        and device.target_type == "aggregator"
                        and not device.no_op
                    ),
                    "listeners": sum(
                        1
                        for device in plan.devices
                        if device.deployment_id == deployment_id
                        and device.target_type == "listener"
                        and not device.no_op
                    ),
                },
                "level": plan.write_level,
            },
        )
    db.commit()
    by_target = {(device.target_type, device.target_id): device for device in plan.devices}
    return ApplyOut(
        state="draft",
        publish_enabled=request.app.state.settings.publish_enabled,
        revisions=[
            AppliedRevisionOut(
                revision_id=revision.id,
                target_type=revision.target_type,
                target_id=revision.target_id,
                deployment_id=revision.deployment_id,
                changed_keys=list(
                    by_target[(revision.target_type, revision.target_id)].changed_keys
                ),
                checksum=revision.checksum,
            )
            for revision in revisions
        ],
    )


@router.get("/catalog", response_model=CatalogOut, dependencies=[Depends(require_any_assignment)])
def get_catalog(db: DbDep) -> CatalogOut:
    rows = db.scalars(select(SettingsCatalog).order_by(SettingsCatalog.key)).all()
    return CatalogOut(
        version=max((row.version for row in rows), default=0),
        items=[
            CatalogItemOut(
                key=row.key,
                value_type=row.value_type,
                enum_values=row.enum_values,
                min_value=row.min_value,
                max_value=row.max_value,
                default=row.default_value,
                lowest_level=row.lowest_level,
                secret=row.secret,
                resolution=row.resolution,
                write_restricted=row.write_restricted,
                notes=row.notes,
            )
            for row in rows
        ],
    )
