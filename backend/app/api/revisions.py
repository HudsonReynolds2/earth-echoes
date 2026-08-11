"""Config-revision read surface (task E2.6; spec 13, 6.2; D55) plus the
operator publish action (task E3.7; spec 6.2, 6.4).

`POST /revisions/{id}/publish` is the operator half of the spec 6.4 loop and
the ONLY way drift gets repaired in this phase: the worker never
auto-republishes (`deployment.auto_reconcile` is stored and inert pending
spec 17 item 3). It is the same `publish_revision` E3.13 wires E2's apply to,
so a single publish and a bulk apply take one code path with one set of
refusals.

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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.hierarchy_common import deployment_of_aggregator, not_found
from app.api.pagination import ListResponse, PageParams, apply_page
from app.auth.deps import DbDep, require_csrf
from app.auth.rbac import Permission, has_permission
from app.controlplane.broker import BrokerUnavailable
from app.controlplane.publisher import (
    PublishDisabled,
    StaleRevision,
    UnknownPublishTarget,
    UnknownRevision,
    publish_revision,
)
from app.errors import AppError
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


class PublishOut(BaseModel):
    """What the publish did. `transitioned` is False on the idempotent
    re-publish path (D72): the retained bytes went out again and the state did
    not move, which is the operator's repair for a broker that lost its
    retained store."""

    revision_id: uuid.UUID
    topic: str
    deployment_id: uuid.UUID
    checksum: str
    state: str
    trigger: str | None
    transitioned: bool
    superseded: list[uuid.UUID]


@router.post("/revisions/{revision_id}/publish", response_model=PublishOut)
async def publish_one_revision(
    revision_id: uuid.UUID,
    request: Request,
    db: DbDep,
    actor: Annotated[UserSession, Depends(require_csrf)],
) -> PublishOut:
    """Publish one revision's desired config, retained (spec 6.4 steps 1-2).

    Two-step scoping, deliberately: a revision the caller cannot SEE answers
    404 (the D35 existence-oracle rule the revision reads already follow),
    and one they can see but may not publish answers 403 naming the
    permission. Collapsing both into 404 would tell a viewer their own
    deployment's revision does not exist.

    Every refusal below comes from `publisher.py` rather than being
    re-derived here; this function only chooses each one's status code.
    """
    row = db.get(ConfigRevision, revision_id)
    if row is None or not has_permission(
        actor.user.role_assignments, Permission.VIEW_STATUS, row.deployment_id
    ):
        raise not_found("revision")
    if not has_permission(actor.user.role_assignments, Permission.MANAGE_CONFIG, row.deployment_id):
        raise AppError(
            "forbidden", "requires permission manage_config in this deployment", status_code=403
        )

    settings = request.app.state.settings
    manager = getattr(request.app.state, "mqtt", None)
    if manager is None:
        # Either publication is off (D61), or this process holds no outbound
        # connection because it was started with it off. Same answer either
        # way, and the flag is the thing an operator can act on.
        raise AppError(
            "conflict",
            "publication is disabled on this platform (EOE_PUBLISH_ENABLED)",
            status_code=409,
        )
    try:
        outcome = await publish_revision(
            request.app.state.session_factory,
            manager,
            revision_id,
            publish_enabled=settings.publish_enabled,
            actor_user_id=actor.user_id,
        )
    except PublishDisabled as error:
        raise AppError("conflict", str(error), status_code=409) from error
    except UnknownRevision as error:
        # Committed between the read above and the publish. Genuinely gone.
        raise not_found("revision") from error
    except (StaleRevision, UnknownPublishTarget) as error:
        raise AppError("conflict", str(error), status_code=409) from error
    except BrokerUnavailable as error:
        # The revision is untouched (the publish rides inside the transaction,
        # D74), so this is honestly retryable and says so.
        raise AppError("service_unavailable", str(error), status_code=503) from error
    return PublishOut(
        revision_id=outcome.revision_id,
        topic=outcome.topic,
        deployment_id=outcome.deployment_id,
        checksum=outcome.checksum,
        state=outcome.state.value,
        trigger=outcome.trigger.value if outcome.trigger is not None else None,
        transitioned=outcome.transitioned,
        superseded=list(outcome.superseded),
    )


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
