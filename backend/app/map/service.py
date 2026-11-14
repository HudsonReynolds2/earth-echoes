"""The map's reads (task E6.4; spec 9.2, 9.3, 14.4; phase-6 fixed choice 5).

Two shapes, one per drill level, so the Organization layer never waits on a
Deployment's devices (spec 14.4: "never block on a slow deployment"):

- `deployments_layer`: one compact row per visible Deployment.
- `deployment_detail`: one Deployment's Pods and Listeners.

**The query count is constant in the number of devices.** Every per-device
fact comes from a batched reader (`hierarchy_common.aggregator_statuses`,
`listener_statuses`, the `alerts` counts), each a fixed number of queries
over an id list. `test_map_endpoints.py` holds this with a statement counter
from one Listener to a thousand.

Status is NOT derived here. Devices come from `hierarchy_common`'s readers,
which are the same ones the inventory tables use (D60), and parents come from
`rollup.roll_up`.
"""

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.hierarchy_common import _latest_revision_states as latest_revision_states
from app.api.hierarchy_common import aggregator_statuses, listener_statuses
from app.config.service import effective_for
from app.controlplane.device_status import DeviceStatus
from app.errors import AppError
from app.inventory.naming import normalize_mac
from app.map.alerts import aggregator_alert_counts, listener_alert_counts, unattached_alert_counts
from app.map.rollup import roll_up
from app.models import (
    Aggregator,
    AggregatorStatus,
    Deployment,
    DeviceState,
    InventoryAlert,
    Listener,
    Pod,
)
from app.scoping import DeploymentScope, scope_filter


class MapDeploymentOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    gps_lat: float | None
    gps_lon: float | None
    status: str
    #: Devices per status, every status present (zeros included), in severity order.
    counts: dict[str, int]
    #: Attached alerts on its devices plus unattached alerts naming it (D191).
    alert_count: int
    services_status: str
    pod_count: int
    listener_count: int


class MapPodOut(BaseModel):
    id: uuid.UUID
    name: str
    aggregator_id: uuid.UUID | None
    aggregator_uuid: str | None
    aggregator_name: str | None
    #: The Pod marker's position IS its Aggregator's (spec 9.2).
    gps_lat: float | None
    gps_lon: float | None
    #: The Aggregator's own status, or None for a Pod with no Aggregator.
    aggregator_status: str | None
    #: Worst of the Aggregator and its Listeners.
    status: str
    counts: dict[str, int]
    alert_count: int
    listener_count: int


class MapListenerOut(BaseModel):
    mac: str
    name: str
    pod_id: uuid.UUID
    aggregator_id: uuid.UUID
    gps_lat: float | None
    gps_lon: float | None
    status: str
    alert_count: int


class MapDeploymentDetailOut(BaseModel):
    deployment: MapDeploymentOut
    pods: list[MapPodOut]
    listeners: list[MapListenerOut]


def _pods_with_aggregators(
    db: Session, deployment_ids: list[uuid.UUID]
) -> list[tuple[Pod, Aggregator | None]]:
    rows = db.execute(
        select(Pod, Aggregator)
        .outerjoin(Aggregator, Aggregator.pod_id == Pod.id)
        .where(Pod.deployment_id.in_(deployment_ids))
        .order_by(Pod.name)
    ).all()
    return [(pod, aggregator) for pod, aggregator in rows]


def _listeners(db: Session, deployment_ids: list[uuid.UUID]) -> list[Listener]:
    return list(
        db.scalars(
            select(Listener)
            .where(Listener.deployment_id.in_(deployment_ids))
            .order_by(Listener.name)
        ).all()
    )


class _Fleet:
    """Every device in a set of Deployments, with displayed status and
    alerts, read in a fixed number of queries."""

    def __init__(self, db: Session, deployment_ids: list[uuid.UUID]) -> None:
        self.pods = _pods_with_aggregators(db, deployment_ids)
        self.listeners = _listeners(db, deployment_ids)
        aggregator_ids = [agg.id for _, agg in self.pods if agg is not None]
        macs = [row.mac for row in self.listeners]
        self.aggregator_status = aggregator_statuses(db, aggregator_ids)
        self.listener_status = listener_statuses(db, macs)
        self.aggregator_alerts = aggregator_alert_counts(db, aggregator_ids)
        self.listener_alerts = listener_alert_counts(db, macs)
        self.unattached = unattached_alert_counts(db, deployment_ids)
        self.pod_of_aggregator = {agg.id: pod.id for pod, agg in self.pods if agg is not None}
        self.listeners_by_pod: dict[uuid.UUID, list[Listener]] = defaultdict(list)
        for row in self.listeners:
            pod_id = self.pod_of_aggregator.get(row.aggregator_id)
            if pod_id is not None:
                self.listeners_by_pod[pod_id].append(row)

    def pod_devices(
        self, pod: Pod, aggregator: Aggregator | None
    ) -> tuple[list[DeviceStatus], int]:
        statuses: list[DeviceStatus] = []
        alerts = 0
        if aggregator is not None:
            statuses.append(self.aggregator_status.get(aggregator.id, DeviceStatus.UNKNOWN))
            alerts += self.aggregator_alerts.get(aggregator.id, 0)
        for row in self.listeners_by_pod.get(pod.id, []):
            statuses.append(self.listener_status.get(row.mac, DeviceStatus.UNKNOWN))
            alerts += self.listener_alerts.get(row.mac, 0)
        return statuses, alerts


def _deployment_row(deployment: Deployment, fleet: _Fleet) -> MapDeploymentOut:
    statuses: list[DeviceStatus] = []
    alerts = fleet.unattached.get(deployment.id, 0)
    pods = [(pod, agg) for pod, agg in fleet.pods if pod.deployment_id == deployment.id]
    for pod, aggregator in pods:
        pod_statuses, pod_alerts = fleet.pod_devices(pod, aggregator)
        statuses.extend(pod_statuses)
        alerts += pod_alerts
    marker = roll_up(statuses, services_status=deployment.services_status)
    return MapDeploymentOut(
        id=deployment.id,
        name=deployment.name,
        slug=deployment.slug,
        gps_lat=deployment.gps_lat,
        gps_lon=deployment.gps_lon,
        status=marker.status.value,
        counts=marker.counts,
        alert_count=alerts,
        services_status=deployment.services_status,
        pod_count=len(pods),
        listener_count=sum(len(fleet.listeners_by_pod.get(pod.id, [])) for pod, _ in pods),
    )


def deployments_layer(db: Session, scope: DeploymentScope) -> list[MapDeploymentOut]:
    """The Organization layer: every visible Deployment, rolled up."""
    deployments = list(
        db.scalars(scope_filter(select(Deployment), Deployment.id, scope).order_by(Deployment.name))
    )
    fleet = _Fleet(db, [row.id for row in deployments])
    return [_deployment_row(row, fleet) for row in deployments]


def deployment_detail(db: Session, deployment: Deployment) -> MapDeploymentDetailOut:
    """One Deployment drilled into: its row, its Pods, its Listeners."""
    fleet = _Fleet(db, [deployment.id])
    pods: list[MapPodOut] = []
    for pod, aggregator in fleet.pods:
        statuses, alerts = fleet.pod_devices(pod, aggregator)
        marker = roll_up(statuses)
        pods.append(
            MapPodOut(
                id=pod.id,
                name=pod.name,
                aggregator_id=aggregator.id if aggregator else None,
                aggregator_uuid=aggregator.aggregator_uuid if aggregator else None,
                aggregator_name=aggregator.name if aggregator else None,
                gps_lat=aggregator.gps_lat if aggregator else None,
                gps_lon=aggregator.gps_lon if aggregator else None,
                aggregator_status=(
                    fleet.aggregator_status.get(aggregator.id, DeviceStatus.UNKNOWN).value
                    if aggregator
                    else None
                ),
                status=marker.status.value,
                counts=marker.counts,
                alert_count=alerts,
                listener_count=len(fleet.listeners_by_pod.get(pod.id, [])),
            )
        )
    listeners = [
        MapListenerOut(
            mac=row.mac,
            name=row.name,
            pod_id=fleet.pod_of_aggregator[row.aggregator_id],
            aggregator_id=row.aggregator_id,
            gps_lat=row.gps_lat,
            gps_lon=row.gps_lon,
            status=fleet.listener_status.get(row.mac, DeviceStatus.UNKNOWN).value,
            alert_count=fleet.listener_alerts.get(row.mac, 0),
        )
        for row in fleet.listeners
        if row.aggregator_id in fleet.pod_of_aggregator
    ]
    return MapDeploymentDetailOut(
        deployment=_deployment_row(deployment, fleet), pods=pods, listeners=listeners
    )


# --- E6.5: one device, for the drawer's Status tab ------------------------------


class MapAlertOut(BaseModel):
    """An open alert, with the detail its writer recorded (E1.5's
    `inventory_alert` today, E7's Grafana alerts later)."""

    id: uuid.UUID
    alert_type: str
    entity_type: str
    entity_key: str
    detail: dict[str, Any] | None
    created_at: datetime


class GraceOut(BaseModel):
    """`listener.wake_grace_seconds` as the platform resolves it for this
    Listener, and the level it is inherited from (spec 6.5, 5.2)."""

    seconds: int | None
    source: str
    source_entity_id: str | None


class LivenessOut(BaseModel):
    """Spec 6.5 liveness exactly as the Aggregator last reported it (E3.9).
    All None when no report has arrived: the drawer says so, never guesses."""

    state: str | None
    last_audio_at: datetime | None
    expected_wake_at: datetime | None
    changed_at: datetime | None
    reported_at: datetime | None
    grace: GraceOut


class MapAggregatorOut(BaseModel):
    """An Aggregator's LWT verdict and latest revision (E3.8, E3.6)."""

    id: uuid.UUID
    name: str | None
    aggregator_uuid: str
    status: str
    #: None when no status message has ever arrived (E3.8 writes no row).
    online: bool | None
    #: When `online` last changed value: "offline since".
    online_changed_at: datetime | None
    revision_state: str | None


DeviceKind = Literal["listener", "aggregator", "pod", "deployment"]


class MapDeviceOut(BaseModel):
    """`GET /map/devices/{entity_type}/{id}`: the Status tab of the drawer.

    One shape for four levels. Sections that do not apply to a level are
    None, so a client renders what is there rather than switching on a union.
    """

    entity_type: DeviceKind
    #: A Listener's MAC, or the platform UUID of the others.
    id: str
    name: str
    status: str
    deployment_id: uuid.UUID
    deployment_name: str
    pod_id: uuid.UUID | None
    pod_name: str | None
    gps_lat: float | None
    gps_lon: float | None
    alerts: list[MapAlertOut]
    #: Listener and Aggregator: the newest revision's state.
    revision_state: str | None = None
    #: Listener only.
    liveness: LivenessOut | None = None
    #: Listener: its Aggregator. Aggregator: itself. Pod: its Aggregator.
    aggregator: MapAggregatorOut | None = None
    #: Pod and Deployment: devices per status, as on the marker.
    counts: dict[str, int] | None = None
    listener_count: int | None = None
    pod_count: int | None = None
    #: Deployment only (E5.5's column; its per-service chips come from E5.5's
    #: own endpoint, which the drawer reads directly).
    services_status: str | None = None


#: The drawer lists alerts; a Deployment with more than this many open is
#: already a page for E7's alert list, not a drawer.
ALERT_LIST_LIMIT = 50


def _open_alerts(db: Session, condition: Any) -> list[MapAlertOut]:
    rows = db.scalars(
        select(InventoryAlert)
        .where(InventoryAlert.resolved_at.is_(None), condition)
        .order_by(InventoryAlert.created_at.desc(), InventoryAlert.id)
        .limit(ALERT_LIST_LIMIT)
    )
    return [
        MapAlertOut(
            id=row.id,
            alert_type=row.alert_type,
            entity_type=row.entity_type,
            entity_key=row.entity_key,
            detail=row.detail,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _listener_alerts(macs: list[str]) -> Any:
    return (InventoryAlert.entity_type == "listener") & InventoryAlert.entity_key.in_(macs)


def _aggregator_alerts(uuids: list[str]) -> Any:
    return (InventoryAlert.entity_type == "aggregator") & InventoryAlert.entity_key.in_(uuids)


def _aggregator_out(db: Session, aggregator: Aggregator) -> MapAggregatorOut:
    status_row = db.scalars(
        select(AggregatorStatus).where(AggregatorStatus.aggregator_id == aggregator.id)
    ).first()
    return MapAggregatorOut(
        id=aggregator.id,
        name=aggregator.name,
        aggregator_uuid=aggregator.aggregator_uuid,
        status=aggregator_statuses(db, [aggregator.id])[aggregator.id].value,
        online=status_row.online if status_row else None,
        online_changed_at=status_row.changed_at if status_row else None,
        revision_state=latest_revision_states(db, "aggregator", [str(aggregator.id)]).get(
            str(aggregator.id)
        ),
    )


def _liveness(db: Session, mac: str) -> LivenessOut:
    state = db.scalars(
        select(DeviceState).where(
            DeviceState.entity_type == "listener", DeviceState.entity_id == mac
        )
    ).first()
    # The REDACTED accessor, like every response; this key is not a secret.
    grace = effective_for(db, "listener", mac).get(GRACE_KEY)
    return LivenessOut(
        state=state.liveness_state if state else None,
        last_audio_at=state.last_audio_at if state else None,
        expected_wake_at=state.expected_wake_at if state else None,
        changed_at=state.liveness_changed_at if state else None,
        reported_at=state.received_at if state else None,
        grace=GraceOut(
            seconds=grace.value if grace else None,
            source=grace.source if grace else "default",
            source_entity_id=grace.source_entity_id if grace else None,
        ),
    )


GRACE_KEY = "listener.wake_grace_seconds"


def resolve_device(
    db: Session, entity_type: DeviceKind, entity_id: str
) -> tuple[uuid.UUID, Listener | Aggregator | Pod | Deployment] | None:
    """The row and its Deployment, or None when it does not exist. The caller
    checks scope and answers one 404 for both (D35)."""
    if entity_type == "listener":
        try:
            mac = normalize_mac(entity_id)
        except AppError:
            return None
        listener = db.get(Listener, mac)
        return (listener.deployment_id, listener) if listener else None
    try:
        key = uuid.UUID(entity_id)
    except ValueError:
        return None
    if entity_type == "aggregator":
        aggregator = db.get(Aggregator, key)
        if aggregator is None:
            return None
        return db.get_one(Pod, aggregator.pod_id).deployment_id, aggregator
    if entity_type == "pod":
        pod = db.get(Pod, key)
        return (pod.deployment_id, pod) if pod else None
    deployment = db.get(Deployment, key)
    return (deployment.id, deployment) if deployment else None


def device_detail(db: Session, row: Listener | Aggregator | Pod | Deployment) -> MapDeviceOut:
    """One device's Status tab. Statuses come from the same readers and the
    same rollup as its marker, so the drawer and the marker cannot disagree."""
    if isinstance(row, Listener):
        aggregator = db.get_one(Aggregator, row.aggregator_id)
        pod = db.get_one(Pod, aggregator.pod_id)
        return MapDeviceOut(
            entity_type="listener",
            id=row.mac,
            name=row.name,
            status=listener_statuses(db, [row.mac])[row.mac].value,
            deployment_id=row.deployment_id,
            deployment_name=db.get_one(Deployment, row.deployment_id).name,
            pod_id=pod.id,
            pod_name=pod.name,
            gps_lat=row.gps_lat,
            gps_lon=row.gps_lon,
            alerts=_open_alerts(db, _listener_alerts([row.mac])),
            revision_state=latest_revision_states(db, "listener", [row.mac]).get(row.mac),
            liveness=_liveness(db, row.mac),
            aggregator=_aggregator_out(db, aggregator),
        )
    if isinstance(row, Aggregator):
        pod = db.get_one(Pod, row.pod_id)
        own = _aggregator_out(db, row)
        return MapDeviceOut(
            entity_type="aggregator",
            id=str(row.id),
            # An Aggregator may be unnamed; its uuid is what the device calls itself.
            name=row.name or row.aggregator_uuid,
            status=own.status,
            deployment_id=pod.deployment_id,
            deployment_name=db.get_one(Deployment, pod.deployment_id).name,
            pod_id=pod.id,
            pod_name=pod.name,
            gps_lat=row.gps_lat,
            gps_lon=row.gps_lon,
            alerts=_open_alerts(db, _aggregator_alerts([row.aggregator_uuid])),
            revision_state=own.revision_state,
            aggregator=own,
        )
    if isinstance(row, Pod):
        fleet = _Fleet(db, [row.deployment_id])
        pod_aggregator = next((agg for pod, agg in fleet.pods if pod.id == row.id), None)
        statuses, _ = fleet.pod_devices(row, pod_aggregator)
        marker = roll_up(statuses)
        macs = [listener.mac for listener in fleet.listeners_by_pod.get(row.id, [])]
        condition = _listener_alerts(macs)
        if pod_aggregator is not None:
            condition = condition | _aggregator_alerts([pod_aggregator.aggregator_uuid])
        return MapDeviceOut(
            entity_type="pod",
            id=str(row.id),
            name=row.name,
            status=marker.status.value,
            deployment_id=row.deployment_id,
            deployment_name=db.get_one(Deployment, row.deployment_id).name,
            pod_id=row.id,
            pod_name=row.name,
            gps_lat=pod_aggregator.gps_lat if pod_aggregator else None,
            gps_lon=pod_aggregator.gps_lon if pod_aggregator else None,
            alerts=_open_alerts(db, condition),
            aggregator=_aggregator_out(db, pod_aggregator) if pod_aggregator else None,
            counts=marker.counts,
            listener_count=len(macs),
        )
    fleet = _Fleet(db, [row.id])
    summary = _deployment_row(row, fleet)
    macs = [listener.mac for listener in fleet.listeners]
    uuids = [agg.aggregator_uuid for _, agg in fleet.pods if agg is not None]
    return MapDeviceOut(
        entity_type="deployment",
        id=str(row.id),
        name=row.name,
        status=summary.status,
        deployment_id=row.id,
        deployment_name=row.name,
        pod_id=None,
        pod_name=None,
        gps_lat=row.gps_lat,
        gps_lon=row.gps_lon,
        # Its devices' alerts, and the unattached ones naming it (D191).
        alerts=_open_alerts(
            db,
            _listener_alerts(macs)
            | _aggregator_alerts(uuids)
            | (InventoryAlert.deployment_id == row.id),
        ),
        counts=summary.counts,
        listener_count=summary.listener_count,
        pod_count=summary.pod_count,
        services_status=row.services_status,
    )
