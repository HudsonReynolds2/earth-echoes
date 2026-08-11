"""Per-device reconciliation timeline (task E3.11; spec 6.3, 13).

Spec 6.3 asks for two surfaces over the same events: "A per-device timeline
view renders this history. An Organization-wide and per-Deployment audit log
renders the same events filtered by scope."

This module is the first. The second already exists and is deliberately NOT
rebuilt here: E0.8's `GET /audit` is that surface, and E3.4/E3.5/E3.7 have
been writing `revision.publish`, `revision.report`, `revision.timeout` and
`revision.drift` rows into it since they landed, each carrying the deployment
in `scope`. A second org-wide log over `reconciliation_event` would be a
second answer to one question, and the two would drift.

The split between them is real rather than incidental. `audit_log` answers
"who did what across this organization" and holds every action in the system,
config edits and user administration included. `reconciliation_event` answers
"what happened to this device" — one row per spec 6.2 transition, written by
`revision_state.transition` and by nothing else, which is what makes a
device's timeline complete rather than merely well-intentioned.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.hierarchy_common import deployment_of_aggregator, not_found
from app.api.pagination import ListResponse, PageParams, SortableColumns, apply_page
from app.auth.deps import DbDep
from app.auth.rbac import Permission, has_permission
from app.inventory.naming import normalize_mac
from app.models import Aggregator, Listener, ReconciliationEvent, User, UserSession
from app.scoping import require_any_assignment

router = APIRouter()


class TimelineEntry(BaseModel):
    """One spec 6.2 transition, as an operator reads it.

    `actor_email` is resolved for display and is None for system-driven
    moves — a timeout, a device report, a drift sweep. That None is
    meaningful and must not be rendered as "unknown": nobody did it, which is
    exactly what `failed(timeout)` means (D70).
    """

    id: uuid.UUID
    at: datetime
    revision_id: uuid.UUID
    from_state: str
    to_state: str
    trigger: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    #: Platform-side: how this revision's config differs from the previous one
    #: for this device. Values come from snapshots, which hold secret markers
    #: rather than plaintext (spec 5.4).
    diff: dict[str, Any] | None
    #: Device- or worker-supplied: key NAMES on a mismatch, error text. Never
    #: device-supplied VALUES.
    detail: dict[str, Any] | None


class TimelineQuery(PageParams):
    """Filters over one device's history. Both are for the same question asked
    two ways: "when did this last break" and "what happened to that revision"."""

    revision_id: uuid.UUID | None = None
    to_state: str | None = None


#: Sortable columns, D7 grammar. `at` only: a timeline has exactly one
#: meaningful order, and offering `to_state` as a sort would produce a screen
#: that looks like a history but is not one.
SORTABLE: SortableColumns = {"at": ReconciliationEvent.at}


def _page(
    db: Session, target_type: str, target_id: str, query: TimelineQuery
) -> ListResponse[TimelineEntry]:
    statement = select(ReconciliationEvent).where(
        ReconciliationEvent.target_type == target_type,
        ReconciliationEvent.target_id == target_id,
    )
    if query.revision_id is not None:
        statement = statement.where(ReconciliationEvent.revision_id == query.revision_id)
    if query.to_state is not None:
        statement = statement.where(ReconciliationEvent.to_state == query.to_state)

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    # Newest first by default: a timeline is read from the present backwards,
    # and the `(target_type, target_id, at)` index serves exactly that.
    windowed = PageParams(limit=query.limit, offset=query.offset, sort=query.sort or "-at")
    rows = list(db.scalars(apply_page(statement, windowed, SORTABLE)).all())

    # One lookup for every actor on the page rather than one per row.
    actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
    emails: dict[uuid.UUID, str] = {}
    if actor_ids:
        emails = {
            user_id: email
            for user_id, email in db.execute(
                select(User.id, User.email).where(User.id.in_(actor_ids))
            ).all()
        }
    return ListResponse(
        items=[
            TimelineEntry(
                id=row.id,
                at=row.at,
                revision_id=row.revision_id,
                from_state=row.from_state,
                to_state=row.to_state,
                trigger=row.trigger,
                actor_user_id=row.actor_user_id,
                # A deleted user leaves their id and loses their name; the
                # transition still happened and must still render.
                actor_email=emails.get(row.actor_user_id) if row.actor_user_id else None,
                diff=row.diff,
                detail=row.detail,
            )
            for row in rows
        ],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


@router.get("/aggregators/{aggregator_id}/timeline", response_model=ListResponse[TimelineEntry])
def aggregator_timeline(
    aggregator_id: uuid.UUID,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[TimelineQuery, Query()],
) -> ListResponse[TimelineEntry]:
    """One Aggregator's history. `VIEW_STATUS`, like every other read of this
    device — the timeline says what happened to it, not what may be done to
    it, and a viewer is exactly who reads it."""
    row = db.get(Aggregator, aggregator_id)
    if row is None:
        raise not_found("aggregator")
    deployment_id = deployment_of_aggregator(db, row)
    if not has_permission(session.user.role_assignments, Permission.VIEW_STATUS, deployment_id):
        raise not_found("aggregator")
    # The PLATFORM UUID, which is what `config_revision.target_id` carries and
    # therefore what the transitions were recorded against (D75).
    return _page(db, "aggregator", str(row.id), query)


@router.get("/listeners/{mac}/timeline", response_model=ListResponse[TimelineEntry])
def listener_timeline(
    mac: str,
    db: DbDep,
    session: Annotated[UserSession, Depends(require_any_assignment)],
    query: Annotated[TimelineQuery, Query()],
) -> ListResponse[TimelineEntry]:
    row = db.get(Listener, normalize_mac(mac))
    if row is None or not has_permission(
        session.user.role_assignments, Permission.VIEW_STATUS, row.deployment_id
    ):
        raise not_found("listener")
    return _page(db, "listener", row.mac, query)
