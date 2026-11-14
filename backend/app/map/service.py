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

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.hierarchy_common import aggregator_statuses, listener_statuses
from app.controlplane.device_status import DeviceStatus
from app.map.alerts import aggregator_alert_counts, listener_alert_counts, unattached_alert_counts
from app.map.rollup import roll_up
from app.models import Aggregator, Deployment, Listener, Pod
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
