"""Config-revision read surface (task E2.6; spec 13, 6.2; D55).

Spec 13 lists these routes but no phase-2 task claimed them; they land here
because E2.8's acceptance ("commits and sees draft revisions listed") needs
them (recorded in D55). Device-targeted entities only - organizations,
deployments, and pods never carry revisions (per-device model, spec 6.1).
Scope follows D35: resolve first, identical 404 for missing and
out-of-scope. Snapshots ship as stored - secret MARKERS carry no secret
material - and every row E2 ever writes is state 'draft'.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.hierarchy_common import deployment_of_aggregator, not_found
from app.api.pagination import ListResponse, PageParams, apply_page
from app.auth.deps import DbDep
from app.auth.rbac import Permission, has_permission
from app.inventory.naming import normalize_mac
from app.models import Aggregator, ConfigRevision, Listener, UserSession
from app.scoping import require_any_assignment

router = APIRouter()

SORTABLE = {"created_at": ConfigRevision.created_at, "state": ConfigRevision.state}


class RevisionListItemOut(BaseModel):
    id: uuid.UUID
    target_type: str
    target_id: str
    deployment_id: uuid.UUID
    schema_version: int
    checksum: str
    state: str
    created_by: uuid.UUID | None
    created_at: datetime


class RevisionOut(RevisionListItemOut):
    snapshot: dict[str, Any]


class RevisionsQuery(PageParams):
    state: str | None = None


def _list_for_target(
    db: DbDep, target_type: str, target_id: str, query: RevisionsQuery
) -> ListResponse[RevisionListItemOut]:
    statement = select(ConfigRevision).where(
        ConfigRevision.target_type == target_type, ConfigRevision.target_id == target_id
    )
    if query.state is not None:
        statement = statement.where(ConfigRevision.state == query.state)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "-created_at")
    rows = db.scalars(apply_page(statement, windowed, SORTABLE)).all()
    return ListResponse(
        items=[RevisionListItemOut.model_validate(row, from_attributes=True) for row in rows],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@router.get(
    "/aggregators/{aggregator_id}/revisions", response_model=ListResponse[RevisionListItemOut]
)
def list_aggregator_revisions(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[RevisionsQuery, Query()],
) -> ListResponse[RevisionListItemOut]:
    row = db.get(Aggregator, aggregator_id)
    if row is None:
        raise not_found("aggregator")
    deployment_id = deployment_of_aggregator(db, row)
    if not has_permission(session.user.role_assignments, Permission.VIEW_STATUS, deployment_id):
        raise not_found("aggregator")
    return _list_for_target(db, "aggregator", str(row.id), query)


@router.get("/listeners/{mac}/revisions", response_model=ListResponse[RevisionListItemOut])
def list_listener_revisions(
    mac: str,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[RevisionsQuery, Query()],
) -> ListResponse[RevisionListItemOut]:
    row = db.get(Listener, normalize_mac(mac))
    if row is None or not has_permission(
        session.user.role_assignments, Permission.VIEW_STATUS, row.deployment_id
    ):
        raise not_found("listener")
    return _list_for_target(db, "listener", row.mac, query)


@router.get("/revisions/{revision_id}", response_model=RevisionOut)
def get_revision(
    revision_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
) -> RevisionOut:
    row = db.get(ConfigRevision, revision_id)
    if row is None or not has_permission(
        session.user.role_assignments, Permission.VIEW_STATUS, row.deployment_id
    ):
        raise not_found("revision")
    return RevisionOut.model_validate(row, from_attributes=True)
