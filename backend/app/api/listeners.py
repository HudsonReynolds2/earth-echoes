"""Listener surface (task E1.2/E1.4; spec 13, 4.2/4.3; DECISIONS D31, D35).

Listeners are addressed by MAC everywhere ("id by MAC", spec 13); the path
value is normalized before lookup so aa-bb-cc-dd-ee-ff and AA:BB:CC:DD:EE:FF
are the same device. Out-of-scope items answer 404, never 403 - MACs are
enumerable, and a 403-on-existing would be an existence oracle (D35). The
deployment_id stamp is computed server-side from the aggregator chain and is
never accepted from the client (D32).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.hierarchy_common import (
    ListenerOut,
    deployment_of_aggregator,
    listener_out,
    not_found,
)
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.errors import AppError
from app.inventory.naming import next_free_name, normalize_mac
from app.models import Aggregator, Listener, UserSession
from app.scoping import require_any_assignment, scope_filter, visible_deployments

router = APIRouter(prefix="/listeners")

SORTABLE = {
    "name": Listener.name,
    "mac": Listener.mac,
    "created_at": Listener.created_at,
}


class ListenersQuery(PageParams):
    aggregator_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None
    name: str | None = None
    mac: str | None = None
    tag: str | None = None


class CreateListenerBody(BaseModel):
    # extra="forbid" makes D32 testable: a client-sent deployment_id (the
    # server-computed stamp) is a 422, never silently ignored.
    model_config = {"extra": "forbid"}

    mac: str
    name: str = Field(min_length=1, max_length=200)
    aggregator_id: uuid.UUID
    gps_lat: float | None = Field(default=None, ge=-90, le=90)
    gps_lon: float | None = Field(default=None, ge=-180, le=180)
    # E1.4 (spec 4.3): the auto-suffix is an EXPLICIT request parameter,
    # never silent. Applies to name collisions only; a MAC collision always
    # rejects - no parameter overrides it.
    auto_suffix: bool = False


class PatchListenerBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    gps_lat: float | None = Field(default=None, ge=-90, le=90)
    gps_lon: float | None = Field(default=None, ge=-180, le=180)


def _resolve(db: DbDep, session: UserSession, mac: str, permission: Permission) -> Listener:
    row = db.get(Listener, normalize_mac(mac))
    if row is None or not has_permission(
        session.user.role_assignments, permission, row.deployment_id
    ):
        raise not_found("listener")
    return row


@router.get("", response_model=ListResponse[ListenerOut])
def list_listeners(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[ListenersQuery, Query()],
) -> ListResponse[ListenerOut]:
    statement = select(Listener)
    if query.aggregator_id is not None:
        statement = statement.where(Listener.aggregator_id == query.aggregator_id)
    if query.deployment_id is not None:
        statement = statement.where(Listener.deployment_id == query.deployment_id)
    if query.name is not None:
        statement = statement.where(Listener.name.icontains(query.name))
    if query.mac is not None:
        statement = statement.where(Listener.mac.istartswith(query.mac.upper()))
    if query.tag is not None:
        statement = statement.where(Listener.tags.contains([query.tag]))
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    statement = scope_filter(statement, Listener.deployment_id, scope)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "name")
    rows = list(db.scalars(apply_page(statement, windowed, SORTABLE)).all())
    return ListResponse(
        items=listener_out(rows), total=total, limit=query.limit, offset=query.offset
    )


@router.get("/{mac}", response_model=ListenerOut)
def get_listener(
    mac: str,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> ListenerOut:
    return listener_out([_resolve(db, session, mac, Permission.VIEW_STATUS)])[0]


@router.post("", response_model=ListenerOut, status_code=201)
def create_listener(
    body: CreateListenerBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> ListenerOut:
    aggregator = db.get(Aggregator, body.aggregator_id)
    if aggregator is None:
        raise AppError("not_found", "aggregator not found", status_code=404)
    deployment_id = deployment_of_aggregator(db, aggregator)
    if not has_permission(actor.user.role_assignments, Permission.MANAGE_DEVICES, deployment_id):
        raise AppError(
            "forbidden", "requires permission manage_devices in this deployment", status_code=403
        )
    mac = normalize_mac(body.mac)
    # MAC collision ALWAYS rejects - a duplicate means a data-entry error or a
    # cloned device (spec 4.3 item 1); no parameter overrides this.
    if db.get(Listener, mac) is not None:
        raise AppError("conflict", f"MAC {mac} is already registered", status_code=409)

    final_name = body.name
    row: Listener | None = None
    # Suffix computation and insert race against concurrent creates: retry
    # once with a recomputed suffix, then surface the conflict (E1.4).
    for attempt in (1, 2):
        collides = (
            db.scalar(
                select(Listener.mac).where(
                    Listener.deployment_id == deployment_id, Listener.name == final_name
                )
            )
            is not None
        )
        if collides:
            if not body.auto_suffix:
                raise AppError(
                    "conflict",
                    f"listener name {body.name!r} already exists in this deployment",
                    status_code=409,
                    detail={
                        "field": "name",
                        "suggestion": next_free_name(db, deployment_id, body.name),
                    },
                )
            final_name = next_free_name(db, deployment_id, body.name)
        row = Listener(
            mac=mac,
            name=final_name,
            aggregator_id=body.aggregator_id,
            deployment_id=deployment_id,  # the D32 stamp: server-computed only
            gps_lat=body.gps_lat,
            gps_lon=body.gps_lon,
        )
        db.add(row)
        try:
            db.flush()
            break
        except IntegrityError as error:
            db.rollback()
            row = None
            if attempt == 2 or not body.auto_suffix:
                raise AppError(
                    "conflict",
                    "MAC already registered, or listener name already exists in this deployment",
                    status_code=409,
                ) from error
    assert row is not None  # both loop exits either broke with a row or raised
    detail: dict[str, object] = {"name": row.name, "aggregator_id": str(row.aggregator_id)}
    if row.name != body.name:
        detail.update({"auto_suffixed": True, "requested_name": body.name, "final_name": row.name})
    record_audit(
        db,
        action="listener.create",
        entity_type="listener",
        entity_id=row.mac,
        actor_user_id=actor.user_id,
        scope=deployment_id,
        detail=detail,
    )
    db.commit()
    db.refresh(row)
    return listener_out([row])[0]


@router.patch("/{mac}", response_model=ListenerOut)
def patch_listener(
    mac: str,
    body: PatchListenerBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> ListenerOut:
    row = _resolve(db, actor, mac, Permission.MANAGE_DEVICES)
    changed = body.model_dump(exclude_unset=True)
    for field in ("name", "gps_lat", "gps_lon"):
        if field in changed:
            setattr(row, field, changed[field])
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict", "listener name already exists in this deployment", status_code=409
        ) from error
    if changed:
        record_audit(
            db,
            action="listener.update",
            entity_type="listener",
            entity_id=row.mac,
            actor_user_id=actor.user_id,
            scope=row.deployment_id,
            detail={"changed": sorted(changed)},
        )
    db.commit()
    db.refresh(row)
    return listener_out([row])[0]


@router.delete("/{mac}", status_code=204)
def delete_listener(
    mac: str,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> None:
    row = _resolve(db, actor, mac, Permission.MANAGE_DEVICES)
    db.delete(row)  # listeners are leaves; nothing blocks
    record_audit(
        db,
        action="listener.delete",
        entity_type="listener",
        entity_id=row.mac,
        actor_user_id=actor.user_id,
        scope=row.deployment_id,
        detail={"name": row.name},
    )
    db.commit()
