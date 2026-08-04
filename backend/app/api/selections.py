"""Selection surface (task E2.5; spec 5.2, 13; DECISIONS D54).

Three routes, exactly spec 13's list: preview (evaluate a query without
saving), list, create. No PATCH, no DELETE - deliberate (D54). Preview
visibility rides VIEW_STATUS (it is a browse tool); creating a selection
needs MANAGE_CONFIG somewhere - org-wide or at least one scoped grant -
because selections exist to feed config changes (a local check, not a new
rbac primitive).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.pagination import ListResponse, PageParams, apply_page
from app.audit import record_audit
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission
from app.config.selection import (
    SelectionQuery,
    evaluate_selection,
    validate_selection_query,
)
from app.errors import AppError
from app.models import Selection, UserSession
from app.scoping import require_any_assignment, visible_deployments

router = APIRouter(prefix="/selections")

SORTABLE = {"name": Selection.name, "created_at": Selection.created_at}


class SelectionsQuery(PageParams):
    name: str | None = None


class CreateSelectionBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    query: SelectionQuery


class SelectionOut(BaseModel):
    id: uuid.UUID
    name: str
    query: dict[str, Any]
    created_by: uuid.UUID | None
    created_at: datetime


class MatchedEntityOut(BaseModel):
    entity_type: str
    entity_id: str
    name: str
    deployment_id: str | None
    tags: list[str]


def _validated(query: SelectionQuery) -> SelectionQuery:
    problems = validate_selection_query(query)
    if problems:
        raise AppError(
            "validation_error",
            "invalid selection query",
            status_code=422,
            detail={"errors": problems},
        )
    return query


@router.post("/preview", response_model=ListResponse[MatchedEntityOut])
def preview_selection(
    body: SelectionQuery,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListResponse[MatchedEntityOut]:
    matched = evaluate_selection(
        db, _validated(body), session.user.role_assignments, Permission.VIEW_STATUS
    )
    window = matched[offset : offset + limit]
    return ListResponse(
        items=[
            MatchedEntityOut(
                entity_type=entity.entity_type,
                entity_id=entity.entity_id,
                name=entity.name,
                deployment_id=entity.deployment_id,
                tags=list(entity.tags),
            )
            for entity in window
        ],
        total=len(matched),
        limit=limit,
        offset=offset,
    )


@router.get(
    "",
    response_model=ListResponse[SelectionOut],
    dependencies=[Depends(require_any_assignment)],
)
def list_selections(
    db: DbDep, query: Annotated[SelectionsQuery, Query()]
) -> ListResponse[SelectionOut]:
    statement = select(Selection)
    if query.name is not None:
        statement = statement.where(Selection.name.icontains(query.name))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "name")
    rows = db.scalars(apply_page(statement, windowed, SORTABLE)).all()
    return ListResponse(
        items=[SelectionOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@router.post("", response_model=SelectionOut, status_code=201)
def create_selection(
    body: CreateSelectionBody,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> SelectionOut:
    if not visible_deployments(actor.user.role_assignments, Permission.MANAGE_CONFIG):
        raise AppError(
            "forbidden",
            "creating a selection requires manage_config in at least one deployment",
            status_code=403,
        )
    validated = _validated(body.query)
    row = Selection(
        name=body.name,
        created_by=actor.user_id,
        query=validated.model_dump(mode="json", exclude_none=True),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise AppError(
            "conflict", f"a selection named {body.name!r} already exists", status_code=409
        ) from error
    record_audit(
        db,
        action="selection.create",
        entity_type="selection",
        entity_id=str(row.id),
        actor_user_id=actor.user_id,
        detail={"name": row.name, "entity_type": validated.entity_type},
    )
    db.commit()
    db.refresh(row)
    return SelectionOut.model_validate(row, from_attributes=True)
