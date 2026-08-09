"""Effective and override endpoints (task E2.4; spec 13, 5.1; D50-D51, D35).

Fifteen thin endpoints - GET config/effective, GET/PUT config/overrides on
each of the five hierarchy entities - over three shared helpers. Scope
discipline is E1's, reused verbatim: organizations are every-role-readable
and org-wide-writable; deployments 403 before lookup; pod/aggregator/
listener resolve first and answer the identical 404 for missing and
out-of-scope alike (the D35 enumeration-oracle defense; MACs are guessable).
Reads carry VIEW_STATUS, writes MANAGE_CONFIG + CSRF. Every response is
REDACTED - set secrets render as the keep sentinel, never markers or
plaintext (D51).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.hierarchy_common import deployment_of_aggregator, not_found
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission, require_permission
from app.config.catalog import CATALOG_VERSION
from app.config.overrides import (
    OverrideValidationError,
    get_overrides,
    put_overrides,
)
from app.config.service import effective_for
from app.config.validation import KEEP_SENTINEL, is_secret_marker
from app.errors import AppError
from app.inventory.naming import normalize_mac
from app.models import Aggregator, Deployment, Listener, Organization, Pod, UserSession
from app.scoping import require_any_assignment

router = APIRouter()


class ResolvedValueOut(BaseModel):
    value: Any
    source: str  # organization|deployment|pod|aggregator|listener|default|inventory
    source_entity_id: str | None


class EffectiveOut(BaseModel):
    entity_type: str
    entity_id: str
    catalog_version: int
    config: dict[str, ResolvedValueOut]


class OverridesOut(BaseModel):
    entity_type: str
    entity_id: str
    catalog_version: int
    overrides: dict[str, Any]


class OverridesBody(BaseModel):
    model_config = {"extra": "forbid"}

    overrides: dict[str, Any]


# --- Shared helpers ----------------------------------------------------------


def _effective_out(db: DbDep, entity_type: str, entity_id: str) -> EffectiveOut:
    config = effective_for(db, entity_type, entity_id)  # REDACTED accessor only
    return EffectiveOut(
        entity_type=entity_type,
        entity_id=entity_id,
        catalog_version=CATALOG_VERSION,
        config={
            key: ResolvedValueOut(
                value=rv.value, source=rv.source, source_entity_id=rv.source_entity_id
            )
            for key, rv in config.items()
        },
    )


def _overrides_out(db: DbDep, entity_type: str, entity_id: str) -> OverridesOut:
    raw = get_overrides(db, entity_type, entity_id)
    return OverridesOut(
        entity_type=entity_type,
        entity_id=entity_id,
        catalog_version=CATALOG_VERSION,
        overrides={
            key: dict(KEEP_SENTINEL) if is_secret_marker(value) else value
            for key, value in raw.items()
        },
    )


def _put(
    db: DbDep,
    request: Request,
    actor: UserSession,
    entity_type: str,
    entity_id: str,
    scope: uuid.UUID | None,
    body: OverridesBody,
) -> OverridesOut:
    store = request.app.state.secret_store
    try:
        change = put_overrides(db, store, entity_type, entity_id, body.overrides)
    except OverrideValidationError as error:
        raise AppError(
            "validation_error",
            "invalid override map",
            status_code=422,
            detail={
                "errors": [
                    {"key": e.key, "code": e.code, "message": e.message} for e in error.errors
                ]
            },
        ) from error
    if change.set_keys or change.unset_keys:
        record_audit(
            db,
            action="config.override_update",
            entity_type=entity_type,
            entity_id=entity_id,
            actor_user_id=actor.user_id,
            scope=scope,
            detail={
                "set": list(change.set_keys),
                "unset": list(change.unset_keys),
                "catalog_version": CATALOG_VERSION,
            },
        )
    db.commit()
    for name in change.secret_names_to_delete:  # D51: only ever AFTER the commit
        store.delete(name)
    return _overrides_out(db, entity_type, entity_id)


def _get_organization(db: DbDep, organization_id: uuid.UUID) -> Organization:
    row = db.get(Organization, organization_id)
    if row is None:
        raise AppError("not_found", "organization not found", status_code=404)
    return row


def _get_deployment(db: DbDep, deployment_id: uuid.UUID) -> Deployment:
    row = db.get(Deployment, deployment_id)
    if row is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    return row


def _resolve_pod(db: DbDep, session: UserSession, pod_id: uuid.UUID, perm: Permission) -> Pod:
    row = db.get(Pod, pod_id)
    if row is None or not has_permission(session.user.role_assignments, perm, row.deployment_id):
        raise not_found("pod")
    return row


def _resolve_aggregator(
    db: DbDep, session: UserSession, aggregator_id: uuid.UUID, perm: Permission
) -> tuple[Aggregator, uuid.UUID]:
    row = db.get(Aggregator, aggregator_id)
    if row is None:
        raise not_found("aggregator")
    deployment_id = deployment_of_aggregator(db, row)
    if not has_permission(session.user.role_assignments, perm, deployment_id):
        raise not_found("aggregator")
    return row, deployment_id


def _resolve_listener(db: DbDep, session: UserSession, mac: str, perm: Permission) -> Listener:
    row = db.get(Listener, normalize_mac(mac))
    if row is None or not has_permission(session.user.role_assignments, perm, row.deployment_id):
        raise not_found("listener")
    return row


# --- Organization (org row is every-role-readable; writes need an org-wide
# --- MANAGE_CONFIG grant - a scoped grant changes one deployment, this
# --- changes all of them) ----------------------------------------------------


@router.get(
    "/organizations/{organization_id}/config/effective",
    response_model=EffectiveOut,
    dependencies=[Depends(require_any_assignment)],
)
def organization_effective(organization_id: uuid.UUID, db: DbDep) -> EffectiveOut:
    row = _get_organization(db, organization_id)
    return _effective_out(db, "organization", str(row.id))


@router.get(
    "/organizations/{organization_id}/config/overrides",
    response_model=OverridesOut,
    dependencies=[Depends(require_any_assignment)],
)
def organization_overrides(organization_id: uuid.UUID, db: DbDep) -> OverridesOut:
    row = _get_organization(db, organization_id)
    return _overrides_out(db, "organization", str(row.id))


@router.put("/organizations/{organization_id}/config/overrides", response_model=OverridesOut)
def put_organization_overrides(
    organization_id: uuid.UUID,
    body: OverridesBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OverridesOut:
    if not has_permission(actor.user.role_assignments, Permission.MANAGE_CONFIG, None):
        raise AppError(
            "forbidden",
            "organization-level config requires an organization-wide manage_config grant",
            status_code=403,
        )
    row = _get_organization(db, organization_id)
    return _put(db, request, actor, "organization", str(row.id), None, body)


# --- Deployment (403 before lookup - deployment ids are not enumerable) ------


@router.get(
    "/deployments/{deployment_id}/config/effective",
    response_model=EffectiveOut,
    dependencies=[Depends(require_permission(Permission.VIEW_STATUS, "deployment_id"))],
)
def deployment_effective(deployment_id: uuid.UUID, db: DbDep) -> EffectiveOut:
    row = _get_deployment(db, deployment_id)
    return _effective_out(db, "deployment", str(row.id))


@router.get(
    "/deployments/{deployment_id}/config/overrides",
    response_model=OverridesOut,
    dependencies=[Depends(require_permission(Permission.VIEW_STATUS, "deployment_id"))],
)
def deployment_overrides(deployment_id: uuid.UUID, db: DbDep) -> OverridesOut:
    row = _get_deployment(db, deployment_id)
    return _overrides_out(db, "deployment", str(row.id))


@router.put(
    "/deployments/{deployment_id}/config/overrides",
    response_model=OverridesOut,
    dependencies=[Depends(require_permission(Permission.MANAGE_CONFIG, "deployment_id"))],
)
def put_deployment_overrides(
    deployment_id: uuid.UUID,
    body: OverridesBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OverridesOut:
    row = _get_deployment(db, deployment_id)
    return _put(db, request, actor, "deployment", str(row.id), row.id, body)


# --- Pod / aggregator / listener (resolve first; identical 404, D35) ---------


@router.get("/pods/{pod_id}/config/effective", response_model=EffectiveOut)
def pod_effective(
    pod_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> EffectiveOut:
    row = _resolve_pod(db, session, pod_id, Permission.VIEW_STATUS)
    return _effective_out(db, "pod", str(row.id))


@router.get("/pods/{pod_id}/config/overrides", response_model=OverridesOut)
def pod_overrides(
    pod_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> OverridesOut:
    row = _resolve_pod(db, session, pod_id, Permission.VIEW_STATUS)
    return _overrides_out(db, "pod", str(row.id))


@router.put("/pods/{pod_id}/config/overrides", response_model=OverridesOut)
def put_pod_overrides(
    pod_id: uuid.UUID,
    body: OverridesBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OverridesOut:
    row = _resolve_pod(db, actor, pod_id, Permission.MANAGE_CONFIG)
    return _put(db, request, actor, "pod", str(row.id), row.deployment_id, body)


@router.get("/aggregators/{aggregator_id}/config/effective", response_model=EffectiveOut)
def aggregator_effective(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> EffectiveOut:
    row, _ = _resolve_aggregator(db, session, aggregator_id, Permission.VIEW_STATUS)
    return _effective_out(db, "aggregator", str(row.id))


@router.get("/aggregators/{aggregator_id}/config/overrides", response_model=OverridesOut)
def aggregator_overrides(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> OverridesOut:
    row, _ = _resolve_aggregator(db, session, aggregator_id, Permission.VIEW_STATUS)
    return _overrides_out(db, "aggregator", str(row.id))


@router.put("/aggregators/{aggregator_id}/config/overrides", response_model=OverridesOut)
def put_aggregator_overrides(
    aggregator_id: uuid.UUID,
    body: OverridesBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OverridesOut:
    row, deployment_id = _resolve_aggregator(db, actor, aggregator_id, Permission.MANAGE_CONFIG)
    return _put(db, request, actor, "aggregator", str(row.id), deployment_id, body)


@router.get("/listeners/{mac}/config/effective", response_model=EffectiveOut)
def listener_effective(
    mac: str,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> EffectiveOut:
    row = _resolve_listener(db, session, mac, Permission.VIEW_STATUS)
    return _effective_out(db, "listener", row.mac)


@router.get("/listeners/{mac}/config/overrides", response_model=OverridesOut)
def listener_overrides(
    mac: str,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> OverridesOut:
    row = _resolve_listener(db, session, mac, Permission.VIEW_STATUS)
    return _overrides_out(db, "listener", row.mac)


@router.put("/listeners/{mac}/config/overrides", response_model=OverridesOut)
def put_listener_overrides(
    mac: str,
    body: OverridesBody,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OverridesOut:
    row = _resolve_listener(db, actor, mac, Permission.MANAGE_CONFIG)
    return _put(db, request, actor, "listener", row.mac, row.deployment_id, body)
