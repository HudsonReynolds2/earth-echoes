"""Hierarchy walk + merge-engine access (task E2.3; DECISIONS D52-D53).

The only module routers should call for effective config. Three accessors,
three audiences (INTERFACES, merge engine section):

- effective_for      -> REDACTED. The default; safe for API/UI/audit.
- effective_raw      -> markers verbatim. Internal: E2.6 revision snapshots.
- effective_resolved -> plaintext secrets. INTERNAL ONLY - E3's publisher
                        and E4's bundle generator. Never reachable over HTTP.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.config.catalog import CATALOG_BY_KEY, CatalogEntry
from app.config.merge import LevelOverrides, ResolvedValue, effective_config
from app.config.merge import redact_secrets as _redact
from app.config.merge import resolve_secret_refs as _resolve
from app.models import Aggregator, Deployment, EntityOverride, Listener, Pod
from app.secrets import SecretStore


def ancestry(db: Session, entity_type: str, entity_id: str) -> list[tuple[str, str]]:
    """[(level, entity_id)] from the organization down to the target,
    resolved through the E1 foreign keys (listener uses its set-once
    deployment stamp, D32). Raises LookupError if any link is missing -
    routers resolve the entity first (D35), so a hole is a data bug."""
    if entity_type == "organization":
        return [("organization", entity_id)]
    if entity_type == "deployment":
        deployment = db.get(Deployment, _as_uuid(entity_id))
        if deployment is None:
            raise LookupError(f"deployment {entity_id} not found")
        return [
            ("organization", str(deployment.organization_id)),
            ("deployment", str(deployment.id)),
        ]
    if entity_type == "pod":
        pod = db.get(Pod, _as_uuid(entity_id))
        if pod is None:
            raise LookupError(f"pod {entity_id} not found")
        return [*ancestry(db, "deployment", str(pod.deployment_id)), ("pod", str(pod.id))]
    if entity_type == "aggregator":
        aggregator = db.get(Aggregator, _as_uuid(entity_id))
        if aggregator is None:
            raise LookupError(f"aggregator {entity_id} not found")
        return [
            *ancestry(db, "pod", str(aggregator.pod_id)),
            ("aggregator", str(aggregator.id)),
        ]
    if entity_type == "listener":
        listener = db.get(Listener, entity_id)
        if listener is None:
            raise LookupError(f"listener {entity_id} not found")
        aggregator = db.get(Aggregator, listener.aggregator_id)
        if aggregator is None:
            raise LookupError(f"aggregator of listener {entity_id} not found")
        return [
            *ancestry(db, "deployment", str(listener.deployment_id)),
            ("pod", str(aggregator.pod_id)),
            ("aggregator", str(aggregator.id)),
            ("listener", listener.mac),
        ]
    raise ValueError(f"unknown entity type {entity_type!r}")


def override_chain(db: Session, entity_type: str, entity_id: str) -> list[LevelOverrides]:
    """The ancestry's override rows as merge-engine links, loaded in ONE
    query; levels with no row are simply absent (transparent)."""
    pairs = ancestry(db, entity_type, entity_id)
    rows = db.scalars(
        select(EntityOverride).where(
            tuple_(EntityOverride.entity_type, EntityOverride.entity_id).in_(pairs)
        )
    ).all()
    by_pair = {(row.entity_type, row.entity_id): dict(row.overrides) for row in rows}
    return [
        LevelOverrides(level=level, entity_id=eid, overrides=by_pair[(level, eid)])
        for level, eid in pairs
        if (level, eid) in by_pair
    ]


def effective_for(
    db: Session,
    entity_type: str,
    entity_id: str,
    *,
    catalog: Mapping[str, CatalogEntry] | None = None,
) -> dict[str, ResolvedValue]:
    """The REDACTED effective config - set secrets render as the keep
    sentinel. This is what every router returns."""
    entries = catalog if catalog is not None else CATALOG_BY_KEY
    return _redact(effective_raw(db, entity_type, entity_id, catalog=entries), entries)


def effective_raw(
    db: Session,
    entity_type: str,
    entity_id: str,
    *,
    catalog: Mapping[str, CatalogEntry] | None = None,
) -> dict[str, ResolvedValue]:
    """Markers verbatim - the revision-snapshot form (E2.6). Marker names
    carry no secret material, but this must still never be a response body:
    responses go through effective_for."""
    entries = catalog if catalog is not None else CATALOG_BY_KEY
    inventory: Mapping[str, Any] | None = None
    inventory_id: str | None = None
    if entity_type == "listener":
        listener = db.get(Listener, entity_id)
        if listener is None:
            raise LookupError(f"listener {entity_id} not found")
        inventory = {
            "identity.name": listener.name,
            "identity.mac": listener.mac,
            "location.gps_lat": listener.gps_lat,
            "location.gps_lon": listener.gps_lon,
        }
        inventory_id = listener.mac
    return effective_config(
        override_chain(db, entity_type, entity_id),
        entries,
        target_level=entity_type,
        inventory=inventory,
        inventory_entity_id=inventory_id,
    )


def effective_resolved(
    db: Session,
    entity_type: str,
    entity_id: str,
    secret_store: SecretStore,
    *,
    catalog: Mapping[str, CatalogEntry] | None = None,
) -> dict[str, ResolvedValue]:
    """Plaintext secrets substituted via SecretStore. INTERNAL ONLY: E3's
    publisher and E4's bundle generator. Wiring this into an HTTP response
    is a security defect, full stop."""
    entries = catalog if catalog is not None else CATALOG_BY_KEY
    return _resolve(
        effective_raw(db, entity_type, entity_id, catalog=entries), entries, secret_store.get
    )


def _as_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise LookupError(f"not a valid entity id: {value!r}") from error
