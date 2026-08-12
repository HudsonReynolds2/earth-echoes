"""Bulk change planning (task E2.6; spec 5.2, 14.4; DECISIONS D55-D56).

ONE code path computes the change plan for preview AND apply - the
structural guarantee behind the E2.6 acceptance "preview matches what apply
then produces": both endpoints call build_change_plan with identical
inputs, and apply simply executes the plan it was handed.

Write-at-level semantics (D56): level="target" writes the change map onto
each matched entity; a named level writes ONCE at the single common
ancestor of the matched set at that level (a split is a 422 naming the
candidate ancestors, never a silent multi-write). The affected set is every
Aggregator and Listener whose chain includes a write target - the honest
blast radius, matched or not (a pod-level write reaches every listener in
the pod). Devices whose effective config would not change are reported
no_op and receive no revision.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.auth.rbac import Permission, has_permission
from app.config.canonical import config_checksum
from app.config.catalog import CATALOG_BY_KEY, LEVELS
from app.config.merge import LevelOverrides, ResolvedValue, effective_config
from app.config.overrides import (
    OverrideValidationError,
    get_overrides,
    put_overrides,
    secret_marker,
)
from app.config.selection import MatchedEntity
from app.config.validation import is_keep_sentinel, is_secret_marker, validate_override_map
from app.models import (
    Aggregator,
    ConfigRevision,
    Deployment,
    EntityOverride,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
)
from app.secrets import SecretStore

_DEPTH = {level: depth for depth, level in enumerate(LEVELS)}


class PlanError(Exception):
    """422-shaped (403 for the org-grant case); the API layer converts."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class DevicePlan:
    target_type: str  # aggregator | listener
    target_id: str
    name: str
    pod_id: str
    pod_name: str
    deployment_id: str
    changed_keys: tuple[str, ...]
    before_raw: dict[str, ResolvedValue]
    after_raw: dict[str, ResolvedValue]

    @property
    def no_op(self) -> bool:
        return not self.changed_keys


@dataclass(frozen=True)
class ChangePlan:
    write_level: str
    write_targets: tuple[tuple[str, str], ...]  # (level, entity_id)
    devices: tuple[DevicePlan, ...]  # ordered (deployment_id, target_type, target_id)


class _Hierarchy:
    """Every table loaded once; ancestry, descendants, and chains in memory."""

    def __init__(self, db: Session):
        self.organizations = {str(row.id): row for row in db.scalars(select(Organization)).all()}
        self.deployments = {str(row.id): row for row in db.scalars(select(Deployment)).all()}
        self.pods = {str(row.id): row for row in db.scalars(select(Pod)).all()}
        self.aggregators = {str(row.id): row for row in db.scalars(select(Aggregator)).all()}
        self.listeners = {row.mac: row for row in db.scalars(select(Listener)).all()}
        self.aggregator_by_pod = {str(row.pod_id): row for row in self.aggregators.values()}
        self.listeners_by_aggregator: dict[str, list[Listener]] = {}
        for listener in self.listeners.values():
            self.listeners_by_aggregator.setdefault(str(listener.aggregator_id), []).append(
                listener
            )

    def ancestry(self, level: str, entity_id: str) -> list[tuple[str, str]]:
        if level == "organization":
            return [("organization", entity_id)]
        if level == "deployment":
            deployment = self.deployments[entity_id]
            return [
                ("organization", str(deployment.organization_id)),
                ("deployment", entity_id),
            ]
        if level == "pod":
            pod = self.pods[entity_id]
            return [*self.ancestry("deployment", str(pod.deployment_id)), ("pod", entity_id)]
        if level == "aggregator":
            aggregator = self.aggregators[entity_id]
            return [*self.ancestry("pod", str(aggregator.pod_id)), ("aggregator", entity_id)]
        listener = self.listeners[entity_id]
        aggregator = self.aggregators[str(listener.aggregator_id)]
        return [*self.ancestry("aggregator", str(aggregator.id)), ("listener", entity_id)]

    def device_descendants(self, level: str, entity_id: str) -> list[tuple[str, str]]:
        """(target_type, target_id) for every device under - or at - the
        entity. Aggregator overrides cascade to their listeners (the chain
        includes the aggregator level), so listeners are aggregator
        descendants."""
        if level == "listener":
            return [("listener", entity_id)]
        if level == "aggregator":
            listeners = self.listeners_by_aggregator.get(entity_id, [])
            return [("aggregator", entity_id), *[("listener", lst.mac) for lst in listeners]]
        if level == "pod":
            aggregator = self.aggregator_by_pod.get(entity_id)
            if aggregator is None:
                return []
            return self.device_descendants("aggregator", str(aggregator.id))
        if level == "deployment":
            devices: list[tuple[str, str]] = []
            for pod in self.pods.values():
                if str(pod.deployment_id) == entity_id:
                    devices.extend(self.device_descendants("pod", str(pod.id)))
            return devices
        devices = []
        for deployment in self.deployments.values():
            if str(deployment.organization_id) == entity_id:
                devices.extend(self.device_descendants("deployment", str(deployment.id)))
        return devices

    def device_context(self, target_type: str, target_id: str) -> tuple[str, str, str, str]:
        """(name, pod_id, pod_name, deployment_id) plumbing for plan rows."""
        if target_type == "listener":
            listener = self.listeners[target_id]
            aggregator = self.aggregators[str(listener.aggregator_id)]
            pod = self.pods[str(aggregator.pod_id)]
            return (listener.name, str(pod.id), pod.name, str(listener.deployment_id))
        aggregator = self.aggregators[target_id]
        pod = self.pods[str(aggregator.pod_id)]
        return (
            aggregator.name or aggregator.aggregator_uuid,
            str(pod.id),
            pod.name,
            str(pod.deployment_id),
        )


def restricted_keys() -> frozenset[str]:
    """The catalog's write-restricted keys, read from `CATALOG_BY_KEY`.

    Lives here rather than being imported from `app/services/` so that E2-owned
    code never depends on E5-owned code; `app.services.projection` asserts its
    own table against the same source, so the two cannot disagree.
    """
    return frozenset(
        key for key, entry in CATALOG_BY_KEY.items() if entry.write_restricted is not None
    )


def build_change_plan(
    db: Session,
    matched: list[MatchedEntity],
    changes: Mapping[str, Any],
    level: str,
    assignments: Iterable[RoleAssignment],
    *,
    allow_write_restricted: bool = False,
) -> ChangePlan:
    """Resolve write targets, validate the change map at the write level,
    and compute per-device before/after through the pure merge engine -
    without writing anything.

    `allow_write_restricted` (E5.7a, phase-5 fixed choice 3) does two things,
    and they are one idea: it permits the twelve service-onboarding keys, and
    it makes the write at each target a **wholesale regeneration** of them
    rather than a merge. Every restricted key currently stored at the target is
    dropped before `changes` is applied, so a service field an operator cleared
    disappears from the deployment's overrides instead of surviving forever.
    Unrestricted keys at the target are untouched either way.

    Off by default, which is what keeps `PUT .../config/overrides` refusing
    these keys with `service_restricted` — proven by its own test rather than
    assumed.
    """
    if not matched:
        raise PlanError("the selection matched no entities")
    hierarchy = _Hierarchy(db)
    targets = _write_targets(hierarchy, matched, level)
    target_levels = sorted({target_level for target_level, _ in targets}, key=_DEPTH.__getitem__)
    errors = [
        error
        for target_level in target_levels
        for error in validate_override_map(
            changes,
            CATALOG_BY_KEY,
            entity_level=target_level,
            allow_write_restricted=allow_write_restricted,
        )
    ]
    if errors:
        raise PlanError(
            "invalid change map for the write level",
            detail={
                "errors": [
                    {"key": e.key, "code": e.code, "message": e.message}
                    for e in sorted(set(errors), key=lambda e: (e.key, e.code))
                ]
            },
        )
    if any(target_level == "organization" for target_level, _ in targets) and not has_permission(
        assignments, Permission.MANAGE_CONFIG, None
    ):
        raise PlanError(
            "organization-level changes require an organization-wide manage_config grant",
            status_code=403,
        )

    # Current override maps for every chain the affected devices touch.
    devices = sorted(
        {
            device
            for target_level, target_id in targets
            for device in hierarchy.device_descendants(target_level, target_id)
        }
    )
    chains = {device: hierarchy.ancestry(*device) for device in devices}
    pairs = sorted({pair for chain in chains.values() for pair in chain} | set(targets))
    rows = (
        db.scalars(
            select(EntityOverride).where(
                tuple_(EntityOverride.entity_type, EntityOverride.entity_id).in_(pairs)
            )
        ).all()
        if pairs
        else []
    )
    current = {(row.entity_type, row.entity_id): dict(row.overrides) for row in rows}
    modified = dict(current)
    # Built once, not per key: `restricted_keys()` walks the whole catalog.
    restricted = restricted_keys() if allow_write_restricted else frozenset()
    for target in targets:
        stored = current.get(target, {})
        base = (
            {key: value for key, value in stored.items() if key not in restricted}
            if allow_write_restricted
            else stored
        )
        modified[target] = {**base, **_stored_form(changes, target, stored)}

    plans = []
    for device in devices:
        target_type, target_id = device
        before = _effective(hierarchy, chains[device], current, target_type, target_id)
        after = _effective(hierarchy, chains[device], modified, target_type, target_id)
        # **Compared through `snapshot_from_raw`, not through the raw maps**
        # (E5.7a; the E2-owned defect phase-5 section 2 names). The raw
        # comparison included keys a Listener's snapshot strips, so ONE services
        # save marked every Listener in the deployment as changed, minted a
        # revision per Listener whose published bytes were byte-identical to the
        # previous one, and published all of them. On a SIM fleet that is ~600
        # pointless revisions and ~600 pointless retained publishes per save.
        # Composing through the same function that builds the payload also makes
        # preview honest: "this listener is unaffected" becomes true.
        before_snapshot = snapshot_from_raw(target_type, before)
        after_snapshot = snapshot_from_raw(target_type, after)
        changed = tuple(
            sorted(
                key for key, value in after_snapshot.items() if before_snapshot.get(key) != value
            )
        )
        name, pod_id, pod_name, deployment_id = hierarchy.device_context(target_type, target_id)
        plans.append(
            DevicePlan(
                target_type=target_type,
                target_id=target_id,
                name=name,
                pod_id=pod_id,
                pod_name=pod_name,
                deployment_id=deployment_id,
                changed_keys=changed,
                before_raw=before,
                after_raw=after,
            )
        )
    plans.sort(key=lambda plan: (plan.deployment_id, plan.target_type, plan.target_id))
    return ChangePlan(write_level=level, write_targets=tuple(targets), devices=tuple(plans))


def _stored_form(
    changes: Mapping[str, Any], target: tuple[str, str], existing: Mapping[str, Any]
) -> dict[str, Any]:
    """The change map AS STORAGE WILL HOLD IT (D51): secret plaintext
    becomes the target's marker, the keep sentinel resolves to the stored
    marker (422 if none exists). Modeling the after-state with plaintext
    would leak it into revision snapshots - the exact defect the gate-36
    suite exists to catch."""
    target_level, target_id = target
    stored: dict[str, Any] = {}
    for key, value in changes.items():
        entry = CATALOG_BY_KEY.get(key)
        if entry is None or not entry.secret:
            stored[key] = value
        elif is_keep_sentinel(value):
            kept = existing.get(key)
            if not is_secret_marker(kept):
                raise PlanError(
                    "invalid change map for the write level",
                    detail={
                        "errors": [
                            {
                                "key": key,
                                "code": "invalid_value",
                                "message": f"{key} has no stored secret to keep at the "
                                f"{target_level} write target; provide a replacement value",
                            }
                        ]
                    },
                )
            stored[key] = kept
        else:
            stored[key] = secret_marker(target_level, target_id, key)
    return stored


def _write_targets(
    hierarchy: _Hierarchy, matched: list[MatchedEntity], level: str
) -> list[tuple[str, str]]:
    if level == "target":
        return sorted({(entity.entity_type, entity.entity_id) for entity in matched})
    ancestors: set[str] = set()
    for entity in matched:
        if _DEPTH[entity.entity_type] < _DEPTH[level]:
            raise PlanError(
                f"cannot write at {level} level for a {entity.entity_type} selection "
                "(the matched entities sit above it)"
            )
        chain = dict(hierarchy.ancestry(entity.entity_type, entity.entity_id))
        ancestors.add(chain[level])
    if len(ancestors) != 1:
        raise PlanError(
            f"the matched set does not share a single {level}",
            detail={"level": level, "ancestors": sorted(ancestors)},
        )
    return [(level, ancestors.pop())]


def _effective(
    hierarchy: _Hierarchy,
    chain_pairs: list[tuple[str, str]],
    overrides: dict[tuple[str, str], dict[str, Any]],
    target_type: str,
    target_id: str,
) -> dict[str, ResolvedValue]:
    chain = [
        LevelOverrides(level=lvl, entity_id=eid, overrides=overrides[(lvl, eid)])
        for lvl, eid in chain_pairs
        if (lvl, eid) in overrides
    ]
    inventory = None
    inventory_id = None
    if target_type == "listener":
        listener = hierarchy.listeners[target_id]
        inventory = {
            "identity.name": listener.name,
            "identity.mac": listener.mac,
            "location.gps_lat": listener.gps_lat,
            "location.gps_lon": listener.gps_lon,
        }
        inventory_id = listener.mac
    return effective_config(
        chain,
        CATALOG_BY_KEY,
        target_level=target_type,
        inventory=inventory,
        inventory_entity_id=inventory_id,
    )


def snapshot_from_raw(target_type: str, raw: Mapping[str, ResolvedValue]) -> dict[str, Any]:
    """The publishable payload body (D55) from a RAW effective map: flat
    values, markers in place. Listener snapshots exclude the write-restricted
    service keys (spec 5.4: they never reach Listener-bound config; the 16.4
    post-connect path carries them to aggregators); aggregator snapshots
    include them.

    Split out from `revision_snapshot` for E3.7's drift sweep, which
    recomputes a device's effective config through `service.effective_raw`
    and needs the SAME composition rule to compare checksums. Two copies of
    this rule would make the drift detector report drift on every listener
    with a service key set - the detector itself drifting, which is the one
    failure a drift detector cannot have.
    """
    return {
        key: rv.value
        for key, rv in raw.items()
        if not (target_type == "listener" and CATALOG_BY_KEY[key].write_restricted is not None)
    }


def revision_snapshot(device: DevicePlan) -> dict[str, Any]:
    """The publishable payload body for one planned device (E2.6)."""
    return snapshot_from_raw(device.target_type, device.after_raw)


def apply_change_plan(
    db: Session,
    secret_store: SecretStore,
    plan: ChangePlan,
    changes: Mapping[str, Any],
    actor_user_id: uuid.UUID | None,
    *,
    allow_write_restricted: bool = False,
) -> tuple[list[ConfigRevision], dict[str, list[str]]]:
    """Stage every write in the caller's transaction: merged override maps
    at each write target, one draft revision per non-no_op device. Returns
    (revision rows, changed keys per deployment for the caller's audit
    rows). The caller audits per deployment and commits ONCE; a failure
    anywhere rolls back everything (the E2.6 transactionality acceptance).

    `allow_write_restricted` must match what `build_change_plan` was given.
    It is a fourth signature rather than the three phase-5 fixed choice 3
    counted, and it is not a fourth MEANING: this function calls
    `put_overrides`, so the flag has to reach it or the plan the caller was
    handed could not be executed. Same default, same one idea — permit the
    twelve keys, and regenerate them wholesale instead of merging.
    """
    to_delete: list[str] = []
    restricted = restricted_keys()
    for target_level, target_id in plan.write_targets:
        existing = get_overrides(db, target_level, target_id)
        if allow_write_restricted:
            existing = {key: value for key, value in existing.items() if key not in restricted}
        merged = {**existing, **dict(changes)}
        try:
            change = put_overrides(
                db,
                secret_store,
                target_level,
                target_id,
                merged,
                allow_write_restricted=allow_write_restricted,
            )
        except OverrideValidationError as error:  # pragma: no cover - plan validated already
            raise PlanError(
                "invalid change map",
                detail={
                    "errors": [
                        {"key": e.key, "code": e.code, "message": e.message} for e in error.errors
                    ]
                },
            ) from error
        to_delete.extend(change.secret_names_to_delete)

    revisions = []
    for device in plan.devices:
        if device.no_op:
            continue
        snapshot = revision_snapshot(device)
        revisions.append(
            ConfigRevision(
                target_type=device.target_type,
                target_id=device.target_id,
                deployment_id=uuid.UUID(device.deployment_id),
                snapshot=snapshot,
                schema_version=1,
                checksum=config_checksum(snapshot),
                state="draft",
                created_by=actor_user_id,
            )
        )
    db.add_all(revisions)
    db.flush()
    changed_by_deployment: dict[str, list[str]] = {}
    for device in plan.devices:
        if device.no_op:
            continue
        merged_keys = changed_by_deployment.setdefault(device.deployment_id, [])
        merged_keys.extend(key for key in device.changed_keys if key not in merged_keys)
    return revisions, changed_by_deployment
