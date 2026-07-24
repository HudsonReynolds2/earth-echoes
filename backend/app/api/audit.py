"""Audit read surface (task E0.8; spec 13): GET /audit with scope, actor, and
action filters, owner-gated by Permission.VIEW_AUDIT. First real consumer of
the D7 list contract."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.pagination import ListResponse, PageParams, apply_page
from app.auth.deps import DbDep
from app.auth.rbac import Permission, require_permission
from app.models import AuditLog

router = APIRouter(
    prefix="/audit", dependencies=[Depends(require_permission(Permission.VIEW_AUDIT))]
)

SORTABLE = {"at": AuditLog.at, "action": AuditLog.action}


class AuditEntry(BaseModel):
    id: uuid.UUID
    at: datetime
    actor_user_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str
    scope: uuid.UUID | None
    detail: dict[str, Any] | None
    request_id: str


class AuditQuery(PageParams):
    """List endpoints extend PageParams with their filters so FastAPI binds a
    single query model; mixing a model with loose query params does not expand
    the model (observed at Gate 8). The pattern for every later list surface."""

    action: str | None = None
    actor: uuid.UUID | None = None
    scope: uuid.UUID | None = None


@router.get("", response_model=ListResponse[AuditEntry])
def list_audit(db: DbDep, query: Annotated[AuditQuery, Query()]) -> ListResponse[AuditEntry]:
    statement = select(AuditLog)
    if query.action is not None:
        statement = statement.where(AuditLog.action == query.action)
    if query.actor is not None:
        statement = statement.where(AuditLog.actor_user_id == query.actor)
    if query.scope is not None:
        statement = statement.where(AuditLog.scope == query.scope)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "-at")
    rows = db.scalars(apply_page(statement, windowed, SORTABLE)).all()
    return ListResponse(
        items=[AuditEntry.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )
