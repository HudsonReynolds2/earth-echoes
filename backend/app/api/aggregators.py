"""Aggregator surface (task E1.2/E1.3; spec 13, 4.2; DECISIONS D35).

One aggregator per pod is the uq_aggregator_pod_id constraint; attaching to
an occupied pod surfaces as 409 conflict. aggregator_uuid is platform-
assigned when omitted (spec 4.2's primary path - the platform never trusts a
device self-declaration).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.hierarchy_common import (
    AggregatorOut,
    TagsBody,
    TagsOut,
    aggregator_out,
    clean_tags,
    deployment_of_aggregator,
    not_found,
    refuse_delete_with_children,
)
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.config.overrides import delete_overrides_for
from app.errors import AppError
from app.models import Aggregator, Listener, Pod, UserSession
from app.scoping import require_any_assignment, scope_filter, visible_deployments

router = APIRouter(prefix="/aggregators")

SORTABLE = {
    "name": Aggregator.name,
    "aggregator_uuid": Aggregator.aggregator_uuid,
    "created_at": Aggregator.created_at,
}


class AggregatorsQuery(PageParams):
    pod_id: uuid.UUID | None = None
    deployment_id: uuid.UUID | None = None
    aggregator_uuid: str | None = None
    name: str | None = None
    tag: str | None = None


class CreateAggregatorBody(BaseModel):
    model_config = {"extra": "forbid"}

    pod_id: uuid.UUID
    aggregator_uuid: str | None = Field(default=None, min_length=1, max_length=64)
    balena_uuid: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=200)


class PatchAggregatorBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, max_length=200)
    balena_uuid: str | None = Field(default=None, max_length=64)


def _resolve(
    db: DbDep, session: UserSession, aggregator_id: uuid.UUID, permission: Permission
) -> tuple[Aggregator, uuid.UUID]:
    row = db.get(Aggregator, aggregator_id)
    if row is None:
        raise not_found("aggregator")
    deployment_id = deployment_of_aggregator(db, row)
    if not has_permission(session.user.role_assignments, permission, deployment_id):
        raise not_found("aggregator")
    return row, deployment_id


@router.get("", response_model=ListResponse[AggregatorOut])
def list_aggregators(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[AggregatorsQuery, Query()],
) -> ListResponse[AggregatorOut]:
    statement = select(Aggregator).join(Pod, Pod.id == Aggregator.pod_id)
    if query.pod_id is not None:
        statement = statement.where(Aggregator.pod_id == query.pod_id)
    if query.deployment_id is not None:
        statement = statement.where(Pod.deployment_id == query.deployment_id)
    if query.aggregator_uuid is not None:
        statement = statement.where(Aggregator.aggregator_uuid == query.aggregator_uuid)
    if query.name is not None:
        statement = statement.where(Aggregator.name.icontains(query.name))
    if query.tag is not None:
        statement = statement.where(Aggregator.tags.contains([query.tag]))
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    statement = scope_filter(statement, Pod.deployment_id, scope)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(
        limit=query.limit, offset=query.offset, sort=query.sort or "aggregator_uuid"
    )
    rows = list(db.scalars(apply_page(statement, windowed, SORTABLE)).all())
    return ListResponse(
        items=aggregator_out(db, rows), total=total, limit=query.limit, offset=query.offset
    )


@router.get("/{aggregator_id}", response_model=AggregatorOut)
def get_aggregator(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> AggregatorOut:
    row, _ = _resolve(db, session, aggregator_id, Permission.VIEW_STATUS)
    return aggregator_out(db, [row])[0]


@router.post("", response_model=AggregatorOut, status_code=201)
def create_aggregator(
    body: CreateAggregatorBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> AggregatorOut:
    pod = db.get(Pod, body.pod_id)
    if pod is None:
        raise AppError("not_found", "pod not found", status_code=404)
    if not has_permission(
        actor.user.role_assignments, Permission.MANAGE_DEVICES, pod.deployment_id
    ):
        raise AppError(
            "forbidden", "requires permission manage_devices in this deployment", status_code=403
        )
    row = Aggregator(
        pod_id=body.pod_id,
        aggregator_uuid=body.aggregator_uuid or uuid.uuid4().hex,
        balena_uuid=body.balena_uuid,
        name=body.name,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict",
            "pod already has its aggregator, or aggregator_uuid already exists",
            status_code=409,
        ) from error
    record_audit(
        db,
        action="aggregator.create",
        entity_type="aggregator",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=pod.deployment_id,
        detail={"aggregator_uuid": row.aggregator_uuid, "pod_id": str(row.pod_id)},
    )
    db.commit()
    db.refresh(row)
    return aggregator_out(db, [row])[0]


@router.patch("/{aggregator_id}", response_model=AggregatorOut)
def patch_aggregator(
    aggregator_id: uuid.UUID,
    body: PatchAggregatorBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> AggregatorOut:
    row, deployment_id = _resolve(db, actor, aggregator_id, Permission.MANAGE_DEVICES)
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed:
        row.name = changed["name"]
    if "balena_uuid" in changed:
        row.balena_uuid = changed["balena_uuid"]
    if changed:
        record_audit(
            db,
            action="aggregator.update",
            entity_type="aggregator",
            entity_id=str(row.id),
            actor_user_id=actor.user_id,
            scope=deployment_id,
            detail={"changed": sorted(changed)},
        )
    db.commit()
    db.refresh(row)
    return aggregator_out(db, [row])[0]


@router.delete("/{aggregator_id}", status_code=204)
def delete_aggregator(
    aggregator_id: uuid.UUID,
    db: DbDep,
    request: Request,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> None:
    row, deployment_id = _resolve(db, actor, aggregator_id, Permission.MANAGE_DEVICES)
    refuse_delete_with_children(
        "aggregator",
        {
            "listeners": db.scalar(select(func.count()).where(Listener.aggregator_id == row.id))
            or 0,
        },
    )
    orphaned_secrets = delete_overrides_for(db, "aggregator", str(row.id))  # E2.4 cleanup (D51)
    db.delete(row)
    record_audit(
        db,
        action="aggregator.delete",
        entity_type="aggregator",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        detail={"aggregator_uuid": row.aggregator_uuid},
    )
    db.commit()
    for name in orphaned_secrets:  # D51: only ever AFTER the commit
        request.app.state.secret_store.delete(name)


@router.get("/{aggregator_id}/tags", response_model=TagsOut)
def get_aggregator_tags(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> TagsOut:
    row, _ = _resolve(db, session, aggregator_id, Permission.VIEW_STATUS)
    return TagsOut(tags=row.tags)


@router.put("/{aggregator_id}/tags", response_model=TagsOut)
def put_aggregator_tags(
    aggregator_id: uuid.UUID,
    body: TagsBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> TagsOut:
    row, deployment_id = _resolve(db, actor, aggregator_id, Permission.MANAGE_DEVICES)
    row.tags = clean_tags(body.tags)
    record_audit(
        db,
        action="aggregator.update",
        entity_type="aggregator",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=deployment_id,
        detail={"changed": ["tags"]},
    )
    db.commit()
    db.refresh(row)
    return TagsOut(tags=row.tags)
