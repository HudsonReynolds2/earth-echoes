"""Shared plumbing for the E1.2 hierarchy routers (spec 13; DECISIONS D34-D36).

Out-schemas (with the child counts the tree UI needs without N+1), the
delete-with-children 409, parent-chain resolution for the D35 scope checks,
and batched child-count queries. Every router in app/api/{organizations,
deployments,pods,aggregators,listeners}.py composes these.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import Aggregator, Deployment, Listener, Organization, Pod

TAG_MAX = 64


class TagsBody(BaseModel):
    """PUT /{entity}/{id}/tags carries the WHOLE tag set - replace, not merge
    (task E1.7)."""

    model_config = {"extra": "forbid"}

    tags: list[str]


class TagsOut(BaseModel):
    tags: list[str]


def clean_tags(tags: list[str]) -> list[str]:
    """Trim, drop empties, reject oversized or control-character tags, dedupe,
    sort - deterministic storage for E2's selection engine."""
    cleaned: set[str] = set()
    for tag in tags:
        stripped = tag.strip()
        if not stripped:
            continue
        if len(stripped) > TAG_MAX or any(ord(ch) < 32 for ch in stripped):
            raise AppError(
                "validation_error",
                f"invalid tag {stripped[:80]!r} (max {TAG_MAX} chars, no control characters)",
                status_code=422,
            )
        cleaned.add(stripped)
    return sorted(cleaned)


# --- Out schemas (D7 list items and item responses) ------------------------


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class DeploymentOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    tags: list[str]
    pod_count: int
    listener_count: int
    created_at: datetime
    updated_at: datetime


class AggregatorOut(BaseModel):
    id: uuid.UUID
    pod_id: uuid.UUID
    aggregator_uuid: str
    balena_uuid: str | None
    name: str | None
    tags: list[str]
    listener_count: int
    created_at: datetime
    updated_at: datetime


class PodOut(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID
    name: str
    tags: list[str]
    aggregator: AggregatorOut | None
    listener_count: int
    created_at: datetime
    updated_at: datetime


class ListenerOut(BaseModel):
    mac: str
    name: str
    aggregator_id: uuid.UUID
    deployment_id: uuid.UUID
    gps_lat: float | None
    gps_lon: float | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


# --- Child counts (batched, one GROUP BY per relation) ----------------------


def pod_counts_by_deployment(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Pod.deployment_id, func.count())
        .where(Pod.deployment_id.in_(ids))
        .group_by(Pod.deployment_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def listener_counts_by_deployment(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Listener.deployment_id, func.count())
        .where(Listener.deployment_id.in_(ids))
        .group_by(Listener.deployment_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def listener_counts_by_pod(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Aggregator.pod_id, func.count(Listener.mac))
        .join(Listener, Listener.aggregator_id == Aggregator.id)
        .where(Aggregator.pod_id.in_(ids))
        .group_by(Aggregator.pod_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def listener_counts_by_aggregator(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Listener.aggregator_id, func.count())
        .where(Listener.aggregator_id.in_(ids))
        .group_by(Listener.aggregator_id)
    ).all()
    return {row[0]: row[1] for row in rows}


# --- Serializer helpers ------------------------------------------------------


def deployment_out(db: Session, rows: list[Deployment]) -> list[DeploymentOut]:
    ids = [row.id for row in rows]
    pods = pod_counts_by_deployment(db, ids)
    listeners = listener_counts_by_deployment(db, ids)
    return [
        DeploymentOut(
            id=row.id,
            organization_id=row.organization_id,
            name=row.name,
            slug=row.slug,
            tags=row.tags,
            pod_count=pods.get(row.id, 0),
            listener_count=listeners.get(row.id, 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def aggregator_out(db: Session, rows: list[Aggregator]) -> list[AggregatorOut]:
    counts = listener_counts_by_aggregator(db, [row.id for row in rows])
    return [
        AggregatorOut(
            id=row.id,
            pod_id=row.pod_id,
            aggregator_uuid=row.aggregator_uuid,
            balena_uuid=row.balena_uuid,
            name=row.name,
            tags=row.tags,
            listener_count=counts.get(row.id, 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def pod_out(db: Session, rows: list[Pod]) -> list[PodOut]:
    counts = listener_counts_by_pod(db, [row.id for row in rows])
    aggregators = {
        agg.pod_id: agg
        for agg in db.scalars(
            select(Aggregator).where(Aggregator.pod_id.in_([row.id for row in rows]))
        ).all()
    }
    return [
        PodOut(
            id=row.id,
            deployment_id=row.deployment_id,
            name=row.name,
            tags=row.tags,
            aggregator=(
                aggregator_out(db, [aggregators[row.id]])[0] if row.id in aggregators else None
            ),
            listener_count=counts.get(row.id, 0),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def listener_out(rows: list[Listener]) -> list[ListenerOut]:
    return [ListenerOut.model_validate(row, from_attributes=True) for row in rows]


# --- Deletion and resolution -------------------------------------------------


def refuse_delete_with_children(kind: str, blockers: dict[str, int]) -> None:
    """DELETE rejects when children exist - 409 with the blockers named
    (phase-1 fixed choice: no cascade deletes in v1)."""
    present = {name: count for name, count in blockers.items() if count}
    if present:
        raise AppError(
            "conflict",
            f"cannot delete a {kind} that still has children",
            status_code=409,
            detail={"children": present},
        )


def not_found(kind: str) -> AppError:
    """The D35 rule: a child item that is missing OR out of scope answers the
    same 404, so an enumerable identifier (a MAC) never becomes an existence
    oracle."""
    return AppError("not_found", f"{kind} not found", status_code=404)


def deployment_of_pod(db: Session, pod: Pod) -> uuid.UUID:
    return pod.deployment_id


def deployment_of_aggregator(db: Session, aggregator: Aggregator) -> uuid.UUID:
    deployment_id = db.scalar(select(Pod.deployment_id).where(Pod.id == aggregator.pod_id))
    if deployment_id is None:  # unreachable with FK integrity; fail loud anyway
        raise AppError("internal_error", "aggregator has no pod", status_code=500)
    return deployment_id


def get_sole_organization(db: Session) -> Organization | None:
    return db.scalars(select(Organization).limit(1)).first()
