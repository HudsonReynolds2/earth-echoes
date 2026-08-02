"""Pod surface (task E1.2; spec 13; DECISIONS D35).

Child-item scope rule (D35): PATCH/DELETE/GET on an out-of-scope pod answer
404, never 403 - the row must be fetched before its deployment is known, and
a 403-on-existing would be an existence oracle. POST answers 403: the client
supplied the deployment id, so denial confirms nothing.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.hierarchy_common import PodOut, not_found, pod_out, refuse_delete_with_children
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.errors import AppError
from app.models import Aggregator, Deployment, Pod, UserSession
from app.scoping import require_any_assignment, scope_filter, visible_deployments

router = APIRouter(prefix="/pods")

SORTABLE = {"name": Pod.name, "created_at": Pod.created_at}


class PodsQuery(PageParams):
    deployment_id: uuid.UUID | None = None
    name: str | None = None
    tag: str | None = None


class CreatePodBody(BaseModel):
    model_config = {"extra": "forbid"}

    deployment_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)


class PatchPodBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)


def _visible_pod(db: DbDep, session: UserSession, pod_id: uuid.UUID) -> Pod:
    row = db.get(Pod, pod_id)
    if row is None or not has_permission(
        session.user.role_assignments, Permission.VIEW_STATUS, row.deployment_id
    ):
        raise not_found("pod")
    return row


def _writable_pod(db: DbDep, session: UserSession, pod_id: uuid.UUID) -> Pod:
    row = db.get(Pod, pod_id)
    if row is None or not has_permission(
        session.user.role_assignments, Permission.MANAGE_DEVICES, row.deployment_id
    ):
        raise not_found("pod")
    return row


@router.get("", response_model=ListResponse[PodOut])
def list_pods(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[PodsQuery, Query()],
) -> ListResponse[PodOut]:
    statement = select(Pod)
    if query.deployment_id is not None:
        statement = statement.where(Pod.deployment_id == query.deployment_id)
    if query.name is not None:
        statement = statement.where(Pod.name.icontains(query.name))
    if query.tag is not None:
        statement = statement.where(Pod.tags.contains([query.tag]))
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    statement = scope_filter(statement, Pod.deployment_id, scope)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "name")
    rows = list(db.scalars(apply_page(statement, windowed, SORTABLE)).all())
    return ListResponse(
        items=pod_out(db, rows), total=total, limit=query.limit, offset=query.offset
    )


@router.get("/{pod_id}", response_model=PodOut)
def get_pod(
    pod_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> PodOut:
    return pod_out(db, [_visible_pod(db, session, pod_id)])[0]


@router.post("", response_model=PodOut, status_code=201)
def create_pod(
    body: CreatePodBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> PodOut:
    # Authorize against the claimed deployment BEFORE any existence lookup:
    # a 403 here confirms nothing the client did not already assert (D35).
    if not has_permission(
        actor.user.role_assignments, Permission.MANAGE_DEVICES, body.deployment_id
    ):
        raise AppError(
            "forbidden", "requires permission manage_devices in this deployment", status_code=403
        )
    if db.get(Deployment, body.deployment_id) is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    row = Pod(deployment_id=body.deployment_id, name=body.name)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict", "pod name already exists in this deployment", status_code=409
        ) from error
    record_audit(
        db,
        action="pod.create",
        entity_type="pod",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=row.deployment_id,
        detail={"name": row.name},
    )
    db.commit()
    db.refresh(row)
    return pod_out(db, [row])[0]


@router.patch("/{pod_id}", response_model=PodOut)
def patch_pod(
    pod_id: uuid.UUID,
    body: PatchPodBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> PodOut:
    row = _writable_pod(db, actor, pod_id)
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed:
        row.name = changed["name"]
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict", "pod name already exists in this deployment", status_code=409
        ) from error
    if changed:
        record_audit(
            db,
            action="pod.update",
            entity_type="pod",
            entity_id=str(row.id),
            actor_user_id=actor.user_id,
            scope=row.deployment_id,
            detail={"changed": sorted(changed)},
        )
    db.commit()
    db.refresh(row)
    return pod_out(db, [row])[0]


@router.delete("/{pod_id}", status_code=204)
def delete_pod(
    pod_id: uuid.UUID,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> None:
    row = _writable_pod(db, actor, pod_id)
    refuse_delete_with_children(
        "pod",
        {
            "aggregators": db.scalar(select(func.count()).where(Aggregator.pod_id == row.id)) or 0,
        },
    )
    db.delete(row)
    record_audit(
        db,
        action="pod.delete",
        entity_type="pod",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=row.deployment_id,
        detail={"name": row.name},
    )
    db.commit()
