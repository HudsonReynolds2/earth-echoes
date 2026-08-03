"""Organization surface (task E1.2; spec 13, 12.1; DECISIONS D34).

Spec 13 lists GET/POST/PATCH and no DELETE for organizations - the phase
doc's "all five entities" wording lost that conflict (project-changes #13).
POST is clamped to a single organization while v1 is single-org (spec 12.1;
project-changes #14): rows plus a scoping filter arrive with multi-org, not a
schema rewrite.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.hierarchy_common import (
    OrganizationOut,
    TagsBody,
    TagsOut,
    clean_tags,
    get_sole_organization,
)
from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, require_permission
from app.errors import AppError
from app.models import Organization, UserSession
from app.scoping import require_any_assignment

router = APIRouter(prefix="/organizations")

SORTABLE = {"name": Organization.name, "created_at": Organization.created_at}


class OrganizationsQuery(PageParams):
    name: str | None = None
    tag: str | None = None


class CreateOrganizationBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)


class PatchOrganizationBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)


def _out(row: Organization) -> OrganizationOut:
    return OrganizationOut.model_validate(row, from_attributes=True)


@router.get(
    "", response_model=ListResponse[OrganizationOut], dependencies=[Depends(require_any_assignment)]
)
def list_organizations(
    db: DbDep, query: Annotated[OrganizationsQuery, Query()]
) -> ListResponse[OrganizationOut]:
    statement = select(Organization)
    if query.name is not None:
        statement = statement.where(Organization.name.icontains(query.name))
    if query.tag is not None:
        statement = statement.where(Organization.tags.contains([query.tag]))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "name")
    rows = db.scalars(apply_page(statement, windowed, SORTABLE)).all()
    return ListResponse(
        items=[_out(row) for row in rows], total=total, limit=query.limit, offset=query.offset
    )


@router.get(
    "/{organization_id}",
    response_model=OrganizationOut,
    dependencies=[Depends(require_any_assignment)],
)
def get_organization(organization_id: uuid.UUID, db: DbDep) -> OrganizationOut:
    row = db.get(Organization, organization_id)
    if row is None:
        raise AppError("not_found", "organization not found", status_code=404)
    return _out(row)


@router.post(
    "",
    response_model=OrganizationOut,
    status_code=201,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES))],
)
def create_organization(
    body: CreateOrganizationBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OrganizationOut:
    if get_sole_organization(db) is not None:
        raise AppError(
            "conflict",
            "an organization already exists; v1 runs a single organization (spec 12.1)",
            status_code=409,
        )
    row = Organization(name=body.name)
    db.add(row)
    db.flush()
    record_audit(
        db,
        action="organization.create",
        entity_type="organization",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        detail={"name": row.name},
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationOut,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES))],
)
def patch_organization(
    organization_id: uuid.UUID,
    body: PatchOrganizationBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> OrganizationOut:
    row = db.get(Organization, organization_id)
    if row is None:
        raise AppError("not_found", "organization not found", status_code=404)
    changed = body.model_dump(exclude_unset=True)
    if "name" in changed:
        row.name = changed["name"]
    if changed:
        record_audit(
            db,
            action="organization.update",
            entity_type="organization",
            entity_id=str(row.id),
            actor_user_id=actor.user_id,
            detail={"changed": sorted(changed)},
        )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.get(
    "/{organization_id}/tags",
    response_model=TagsOut,
    dependencies=[Depends(require_any_assignment)],
)
def get_organization_tags(organization_id: uuid.UUID, db: DbDep) -> TagsOut:
    row = db.get(Organization, organization_id)
    if row is None:
        raise AppError("not_found", "organization not found", status_code=404)
    return TagsOut(tags=row.tags)


@router.put(
    "/{organization_id}/tags",
    response_model=TagsOut,
    dependencies=[Depends(require_permission(Permission.MANAGE_DEVICES))],
)
def put_organization_tags(
    organization_id: uuid.UUID,
    body: TagsBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> TagsOut:
    row = db.get(Organization, organization_id)
    if row is None:
        raise AppError("not_found", "organization not found", status_code=404)
    row.tags = clean_tags(body.tags)
    record_audit(
        db,
        action="organization.update",
        entity_type="organization",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        detail={"changed": ["tags"]},
    )
    db.commit()
    db.refresh(row)
    return TagsOut(tags=row.tags)
