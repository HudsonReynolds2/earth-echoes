"""Deployment surface (task E1.2; spec 13, 7.2; DECISIONS D35, D36).

The slug is generated from the name when not supplied and FROZEN once the
deployment has any pod (D36): the {dep} MQTT topic segment must never change
under live topics, and "first use" concretely means "first child pod".
Item routes keep E0.7's require_permission(X, "deployment_id") 403 pattern -
the check runs before any lookup, so it never confirms existence.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.hierarchy_common import (
    DeploymentOut,
    deployment_out,
    get_sole_organization,
    refuse_delete_with_children,
)
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, require_permission
from app.errors import AppError
from app.inventory.naming import next_free_slug, slugify
from app.models import Deployment, Pod, RoleAssignment, UserSession
from app.scoping import require_any_assignment, scope_filter, visible_deployments

router = APIRouter(prefix="/deployments")

SORTABLE = {
    "name": Deployment.name,
    "slug": Deployment.slug,
    "created_at": Deployment.created_at,
}


class DeploymentsQuery(PageParams):
    organization_id: uuid.UUID | None = None
    name: str | None = None
    slug: str | None = None
    tag: str | None = None


class CreateDeploymentBody(BaseModel):
    model_config = {"extra": "forbid"}

    organization_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=63)


class PatchDeploymentBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(default=None, min_length=1, max_length=63)


@router.get("", response_model=ListResponse[DeploymentOut])
def list_deployments(
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[DeploymentsQuery, Query()],
) -> ListResponse[DeploymentOut]:
    statement = select(Deployment)
    if query.organization_id is not None:
        statement = statement.where(Deployment.organization_id == query.organization_id)
    if query.name is not None:
        statement = statement.where(Deployment.name.icontains(query.name))
    if query.slug is not None:
        statement = statement.where(Deployment.slug == query.slug)
    if query.tag is not None:
        statement = statement.where(Deployment.tags.contains([query.tag]))
    scope = visible_deployments(session.user.role_assignments, Permission.VIEW_STATUS)
    statement = scope_filter(statement, Deployment.id, scope)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "name")
    rows = list(db.scalars(apply_page(statement, windowed, SORTABLE)).all())
    return ListResponse(
        items=deployment_out(db, rows), total=total, limit=query.limit, offset=query.offset
    )


@router.get(
    "/{deployment_id}",
    response_model=DeploymentOut,
    dependencies=[Depends(require_permission(Permission.VIEW_STATUS, "deployment_id"))],
)
def get_deployment(deployment_id: uuid.UUID, db: DbDep) -> DeploymentOut:
    row = db.get(Deployment, deployment_id)
    if row is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    return deployment_out(db, [row])[0]


@router.post(
    "",
    response_model=DeploymentOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES))],
)
def create_deployment(
    body: CreateDeploymentBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> DeploymentOut:
    organization = get_sole_organization(db)
    if organization is None or organization.id != body.organization_id:
        raise AppError("not_found", "organization not found", status_code=404)
    slug = body.slug if body.slug is not None else next_free_slug(db, slugify(body.name))
    row = Deployment(organization_id=body.organization_id, name=body.name, slug=slug)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict",
            "deployment name or slug already exists in this organization",
            status_code=409,
        ) from error
    record_audit(
        db,
        action="deployment.create",
        entity_type="deployment",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=row.id,
        detail={"name": row.name, "slug": row.slug},
    )
    db.commit()
    db.refresh(row)
    return deployment_out(db, [row])[0]


@router.patch(
    "/{deployment_id}",
    response_model=DeploymentOut,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES, "deployment_id"))],
)
def patch_deployment(
    deployment_id: uuid.UUID,
    body: PatchDeploymentBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> DeploymentOut:
    row = db.get(Deployment, deployment_id)
    if row is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    changed = body.model_dump(exclude_unset=True)
    if "slug" in changed and changed["slug"] != row.slug:
        pod_count = db.scalar(select(func.count()).where(Pod.deployment_id == row.id)) or 0
        if pod_count:
            raise AppError(
                "conflict",
                "slug is frozen once the deployment has pods (D36; the {dep} "
                "MQTT namespace must not change under live topics)",
                status_code=409,
            )
        row.slug = changed["slug"]
    if "name" in changed:
        row.name = changed["name"]
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict",
            "deployment name or slug already exists in this organization",
            status_code=409,
        ) from error
    if changed:
        record_audit(
            db,
            action="deployment.update",
            entity_type="deployment",
            entity_id=str(row.id),
            actor_user_id=actor.user_id,
            scope=row.id,
            detail={"changed": sorted(changed)},
        )
    db.commit()
    db.refresh(row)
    return deployment_out(db, [row])[0]


@router.delete(
    "/{deployment_id}",
    status_code=204,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES, "deployment_id"))],
)
def delete_deployment(
    deployment_id: uuid.UUID,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> None:
    row = db.get(Deployment, deployment_id)
    if row is None:
        raise AppError("not_found", "deployment not found", status_code=404)
    refuse_delete_with_children(
        "deployment",
        {
            "pods": db.scalar(select(func.count()).where(Pod.deployment_id == row.id)) or 0,
            # Role assignments block too (D33): deleting them implicitly would
            # change who can access what as a side effect of an inventory call.
            "role_assignments": db.scalar(
                select(func.count()).where(RoleAssignment.deployment_id == row.id)
            )
            or 0,
        },
    )
    db.delete(row)
    record_audit(
        db,
        action="deployment.delete",
        entity_type="deployment",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        scope=row.id,
        detail={"name": row.name, "slug": row.slug},
    )
    db.commit()
