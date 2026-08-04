"""Selection grammar and evaluator (task E2.5; spec 5.2, 13; DECISIONS D54).

Structured JSON, not a text DSL (phase-2 fixed choice). The grammar is
Pydantic-validated (every model extra="forbid"), capped (depth 5, 50
predicates), and evaluated in two stages: a SQL prefilter (entity type,
scope, visibility) and a per-candidate Python pass through the E2.3 merge
engine for setting predicates - sanctioned by the phase doc at v1 scale.
Saved selections store the QUERY and re-evaluate at use, never a
materialized id list; every evaluation re-filters through the caller's
visible_deployments, so a stale grant never leaks a device.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.auth.rbac import Permission
from app.config.catalog import CATALOG_BY_KEY, LEVELS
from app.config.merge import LevelOverrides, ResolvedValue, effective_config
from app.inventory.naming import normalize_mac
from app.models import (
    Aggregator,
    Deployment,
    EntityOverride,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
)
from app.scoping import visible_deployments

MAX_DEPTH = 5
MAX_PREDICATES = 50

EntityType = Literal["organization", "deployment", "pod", "aggregator", "listener"]


class TagPredicate(BaseModel):
    """Exact, case-sensitive membership in the entity's own tags - the same
    containment semantics as E1.7's tag= list filter."""

    model_config = {"extra": "forbid"}

    tag: str = Field(min_length=1, max_length=64)


class KeyPredicate(BaseModel):
    """Compares the entity's EFFECTIVE value (inheritance included)."""

    model_config = {"extra": "forbid"}

    key: str
    op: Literal["eq", "ne", "in"]
    value: Any


class ExistsPredicate(BaseModel):
    """True iff an override for the key exists at the entity or ANY ancestor
    - equivalently, effective provenance names one of the five levels (not
    "default", not "inventory"; inventory keys therefore always answer
    False). Allowed on secret keys: set-ness is not a value (D54)."""

    model_config = {"extra": "forbid"}

    key: str
    op: Literal["exists"]


class IdsPredicate(BaseModel):
    """Explicit-identity membership over the selection's entity type - the
    spec 5.2 checkbox path (D54). Listener ids are MACs, normalized before
    comparison."""

    model_config = {"extra": "forbid"}

    ids: list[str] = Field(min_length=1, max_length=1000)


class AllNode(BaseModel):
    model_config = {"extra": "forbid"}

    all: list["Node"] = Field(min_length=1)


class AnyNode(BaseModel):
    model_config = {"extra": "forbid"}

    any: list["Node"] = Field(min_length=1)


Node = AllNode | AnyNode | TagPredicate | KeyPredicate | ExistsPredicate | IdsPredicate
AllNode.model_rebuild()
AnyNode.model_rebuild()


class SelectionScope(BaseModel):
    model_config = {"extra": "forbid"}

    deployment_id: str


class SelectionQuery(BaseModel):
    model_config = {"extra": "forbid"}

    entity_type: EntityType
    scope: SelectionScope | None = None
    where: Node | None = None


@dataclass(frozen=True)
class MatchedEntity:
    entity_type: str
    entity_id: str
    name: str
    deployment_id: str | None
    tags: tuple[str, ...]


def validate_selection_query(query: SelectionQuery) -> list[str]:
    """Semantic checks beyond the Pydantic shape: caps, unknown keys, secret
    value queries. Returns messages (each naming its key); the API folds
    them into one 422."""
    errors: list[str] = []
    predicates = 0

    def walk(node: Node, depth: int) -> None:
        nonlocal predicates
        if depth > MAX_DEPTH:
            errors.append(f"nesting deeper than {MAX_DEPTH} levels")
            return
        if isinstance(node, AllNode | AnyNode):
            children = node.all if isinstance(node, AllNode) else node.any
            for child in children:
                walk(child, depth + 1)
            return
        predicates += 1
        if isinstance(node, KeyPredicate | ExistsPredicate):
            entry = CATALOG_BY_KEY.get(node.key)
            if entry is None:
                errors.append(f"unknown setting key {node.key!r}")
            elif entry.secret and isinstance(node, KeyPredicate):
                errors.append(f"{node.key} is secret; values cannot be queried (exists is allowed)")
        if isinstance(node, KeyPredicate) and node.op == "in" and not isinstance(node.value, list):
            errors.append(f"{node.key}: op 'in' takes a list value")

    if query.where is not None:
        walk(query.where, 1)
    if predicates > MAX_PREDICATES:
        errors.append(f"more than {MAX_PREDICATES} predicates")
    return sorted(set(errors))


def evaluate_selection(
    db: Session,
    query: SelectionQuery,
    assignments: Iterable[RoleAssignment],
    permission: Permission,
) -> list[MatchedEntity]:
    """SQL prefilter (type, scope, visibility) then per-candidate predicate
    evaluation. ALWAYS re-filters through visible_deployments(permission) at
    evaluation time - the property that keeps saved selections and preview/
    apply honest per actor (D54). Deterministic order: entity_id ascending."""
    visible = visible_deployments(assignments, permission)
    candidates = _candidates(db, query, visible)
    if not candidates:
        return []
    effective = (
        _bulk_effective(db, query.entity_type, candidates)
        if query.where is not None and _needs_config(query.where)
        else {}
    )
    matched = [
        entity
        for entity in candidates
        if query.where is None or _matches(query.where, entity, effective.get(entity.entity_id, {}))
    ]
    return sorted(matched, key=lambda entity: entity.entity_id)


# --- Candidates --------------------------------------------------------------


def _candidates(db: Session, query: SelectionQuery, visible: Any) -> list[MatchedEntity]:
    scope_id = query.scope.deployment_id if query.scope is not None else None
    if query.entity_type == "organization":
        rows = db.scalars(select(Organization)).all()
        return [
            MatchedEntity("organization", str(row.id), row.name, None, tuple(row.tags))
            for row in rows
        ]
    if query.entity_type == "deployment":
        dep_stmt = select(Deployment)
        if scope_id is not None:
            dep_stmt = dep_stmt.where(Deployment.id == scope_id)
        if visible != "all":
            dep_stmt = dep_stmt.where(Deployment.id.in_(visible or []))
        return [
            MatchedEntity("deployment", str(row.id), row.name, str(row.id), tuple(row.tags))
            for row in db.scalars(dep_stmt).all()
        ]
    if query.entity_type == "pod":
        pod_stmt = select(Pod)
        if scope_id is not None:
            pod_stmt = pod_stmt.where(Pod.deployment_id == scope_id)
        if visible != "all":
            pod_stmt = pod_stmt.where(Pod.deployment_id.in_(visible or []))
        return [
            MatchedEntity("pod", str(row.id), row.name, str(row.deployment_id), tuple(row.tags))
            for row in db.scalars(pod_stmt).all()
        ]
    if query.entity_type == "aggregator":
        agg_stmt = select(Aggregator, Pod.deployment_id).join(Pod, Aggregator.pod_id == Pod.id)
        if scope_id is not None:
            agg_stmt = agg_stmt.where(Pod.deployment_id == scope_id)
        if visible != "all":
            agg_stmt = agg_stmt.where(Pod.deployment_id.in_(visible or []))
        return [
            MatchedEntity(
                "aggregator",
                str(row.id),
                row.name or row.aggregator_uuid,
                str(deployment_id),
                tuple(row.tags),
            )
            for row, deployment_id in db.execute(agg_stmt).all()
        ]
    lst_stmt = select(Listener)
    if scope_id is not None:
        lst_stmt = lst_stmt.where(Listener.deployment_id == scope_id)
    if visible != "all":
        lst_stmt = lst_stmt.where(Listener.deployment_id.in_(visible or []))
    return [
        MatchedEntity("listener", row.mac, row.name, str(row.deployment_id), tuple(row.tags))
        for row in db.scalars(lst_stmt).all()
    ]


# --- Predicate evaluation ----------------------------------------------------


def _needs_config(node: Node) -> bool:
    if isinstance(node, AllNode):
        return any(_needs_config(child) for child in node.all)
    if isinstance(node, AnyNode):
        return any(_needs_config(child) for child in node.any)
    return isinstance(node, KeyPredicate | ExistsPredicate)


def _matches(node: Node, entity: MatchedEntity, effective: dict[str, ResolvedValue]) -> bool:
    if isinstance(node, AllNode):
        return all(_matches(child, entity, effective) for child in node.all)
    if isinstance(node, AnyNode):
        return any(_matches(child, entity, effective) for child in node.any)
    if isinstance(node, TagPredicate):
        return node.tag in entity.tags
    if isinstance(node, IdsPredicate):
        ids = node.ids
        if entity.entity_type == "listener":
            ids = [normalize_mac(value) for value in ids]
        return entity.entity_id in ids
    if isinstance(node, ExistsPredicate):
        resolved = effective.get(node.key)
        return resolved is not None and resolved.source in LEVELS
    resolved = effective.get(node.key)
    value = resolved.value if resolved is not None else None
    if node.op == "eq":
        return bool(value == node.value)
    if node.op == "ne":
        return bool(value != node.value)
    return isinstance(node.value, list) and value in node.value


# --- Bulk effective config (a handful of queries regardless of N) ------------


def _bulk_effective(
    db: Session, entity_type: str, candidates: list[MatchedEntity]
) -> dict[str, dict[str, ResolvedValue]]:
    """Effective config for every candidate without per-candidate queries:
    ancestor tables load once, all override rows load in one tuple-IN query,
    and the pure merge runs in memory."""
    deployments = {str(row.id): row for row in db.scalars(select(Deployment)).all()}
    pods = {str(row.id): row for row in db.scalars(select(Pod)).all()}
    aggregators = {str(row.id): row for row in db.scalars(select(Aggregator)).all()}
    listeners: dict[str, Listener] = {}
    if entity_type == "listener":
        listeners = {row.mac: row for row in db.scalars(select(Listener)).all()}

    ancestries: dict[str, list[tuple[str, str]]] = {}
    for entity in candidates:
        ancestries[entity.entity_id] = _ancestry_pairs(
            entity, deployments, pods, aggregators, listeners
        )
    pairs = sorted({pair for chain in ancestries.values() for pair in chain})
    override_rows = (
        db.scalars(
            select(EntityOverride).where(
                tuple_(EntityOverride.entity_type, EntityOverride.entity_id).in_(pairs)
            )
        ).all()
        if pairs
        else []
    )
    overrides = {(row.entity_type, row.entity_id): dict(row.overrides) for row in override_rows}

    results: dict[str, dict[str, ResolvedValue]] = {}
    for entity in candidates:
        chain = [
            LevelOverrides(level=level, entity_id=eid, overrides=overrides[(level, eid)])
            for level, eid in ancestries[entity.entity_id]
            if (level, eid) in overrides
        ]
        inventory = None
        inventory_id = None
        if entity.entity_type == "listener":
            listener = listeners[entity.entity_id]
            inventory = {
                "identity.name": listener.name,
                "identity.mac": listener.mac,
                "location.gps_lat": listener.gps_lat,
                "location.gps_lon": listener.gps_lon,
            }
            inventory_id = listener.mac
        results[entity.entity_id] = effective_config(
            chain,
            CATALOG_BY_KEY,
            target_level=entity.entity_type,
            inventory=inventory,
            inventory_entity_id=inventory_id,
        )
    return results


def _ancestry_pairs(
    entity: MatchedEntity,
    deployments: dict[str, Deployment],
    pods: dict[str, Pod],
    aggregators: dict[str, Aggregator],
    listeners: dict[str, Listener],
) -> list[tuple[str, str]]:
    if entity.entity_type == "organization":
        return [("organization", entity.entity_id)]
    if entity.entity_type == "deployment":
        deployment = deployments[entity.entity_id]
        return [
            ("organization", str(deployment.organization_id)),
            ("deployment", entity.entity_id),
        ]
    if entity.entity_type == "pod":
        pod = pods[entity.entity_id]
        deployment = deployments[str(pod.deployment_id)]
        return [
            ("organization", str(deployment.organization_id)),
            ("deployment", str(deployment.id)),
            ("pod", entity.entity_id),
        ]
    if entity.entity_type == "aggregator":
        aggregator = aggregators[entity.entity_id]
        pod = pods[str(aggregator.pod_id)]
        deployment = deployments[str(pod.deployment_id)]
        return [
            ("organization", str(deployment.organization_id)),
            ("deployment", str(deployment.id)),
            ("pod", str(pod.id)),
            ("aggregator", entity.entity_id),
        ]
    listener = listeners[entity.entity_id]
    aggregator = aggregators[str(listener.aggregator_id)]
    pod = pods[str(aggregator.pod_id)]
    deployment = deployments[str(listener.deployment_id)]
    return [
        ("organization", str(deployment.organization_id)),
        ("deployment", str(deployment.id)),
        ("pod", str(pod.id)),
        ("aggregator", str(aggregator.id)),
        ("listener", entity.entity_id),
    ]
