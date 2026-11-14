/**
 * The map's API responses for tests (E6.4's shapes), derived from the
 * inventory fixture so names, ids and coordinates agree with every other
 * screen's tests. Deliberate variety, so each marker rule has something to
 * bite on:
 *
 * - Redwood Coast: Ridge Line's Aggregator is offline, so its Pod and the
 *   Deployment roll up offline; one Alder Creek Listener has an open alert;
 *   Tarn Meadow's Aggregator has no coordinates (an unplaced Pod).
 * - High Desert: nothing has spoken (all `unknown`); services not verified.
 * - "Unplaced Ridge": a Deployment with no coordinates at all.
 */
import type {
  MapAggregator,
  MapDeployment,
  MapDeploymentDetail,
  MapDevice,
  MapListener,
  MapPod,
  StatusCounts,
} from "../src/lib/map";
import type { RolledUpStatus } from "../src/components/StatusChip";
import { STATUS_SEVERITY, worstStatus } from "../src/components/StatusChip";
import { aggregators, deployments, FIXTURE_IDS, listeners, pods } from "./inventory-fixture";

export const UNPLACED_DEPLOYMENT_ID = "d3000000-0000-4000-8000-000000000003";
export const ALERTING_MAC = "02:EE:0E:01:01:03";

const OFFLINE_AGG = "demo-agg-rc-02";
const UNPLACED_AGG = "demo-agg-rc-03";

function counts(statuses: RolledUpStatus[]): StatusCounts {
  const out = Object.fromEntries(STATUS_SEVERITY.map((s) => [s, 0])) as StatusCounts;
  for (const status of statuses) out[status] += 1;
  return out;
}

function listenerStatus(mac: string): RolledUpStatus {
  return mac === ALERTING_MAC ? "alerting" : "unknown";
}

function detailFor(deploymentId: string): MapDeploymentDetail | null {
  const deployment = deployments.find((row) => row.id === deploymentId);
  if (!deployment) return null;
  const podRows = pods.filter((row) => row.deployment_id === deploymentId);
  const mapListeners: MapListener[] = [];
  const mapPods: MapPod[] = podRows.map((pod) => {
    const agg = aggregators.find((row) => row.pod_id === pod.id)!;
    const aggStatus: RolledUpStatus = agg.aggregator_uuid === OFFLINE_AGG ? "offline" : "unknown";
    const own = listeners.filter((row) => row.aggregator_id === agg.id);
    for (const row of own) {
      mapListeners.push({
        mac: row.mac,
        name: row.name,
        pod_id: pod.id,
        aggregator_id: agg.id,
        gps_lat: row.gps_lat,
        gps_lon: row.gps_lon,
        status: listenerStatus(row.mac),
        alert_count: row.mac === ALERTING_MAC ? 1 : 0,
      });
    }
    const statuses = [aggStatus, ...own.map((row) => listenerStatus(row.mac))];
    const placed = agg.aggregator_uuid !== UNPLACED_AGG;
    return {
      id: pod.id,
      name: pod.name,
      aggregator_id: agg.id,
      aggregator_uuid: agg.aggregator_uuid,
      aggregator_name: null,
      gps_lat: placed ? agg.gps_lat : null,
      gps_lon: placed ? agg.gps_lon : null,
      aggregator_status: aggStatus,
      status: worstStatus(statuses),
      counts: counts(statuses),
      alert_count: own.filter((row) => row.mac === ALERTING_MAC).length,
      listener_count: own.length,
    };
  });
  const all = mapPods.flatMap((pod) =>
    STATUS_SEVERITY.flatMap((status) => Array(pod.counts[status]).fill(status) as RolledUpStatus[]),
  );
  return {
    deployment: {
      id: deployment.id,
      name: deployment.name,
      slug: deployment.slug,
      gps_lat: deployment.gps_lat,
      gps_lon: deployment.gps_lon,
      status: worstStatus(all),
      counts: counts(all),
      alert_count: mapPods.reduce((sum, pod) => sum + pod.alert_count, 0),
      services_status:
        deploymentId === FIXTURE_IDS.highDesert ? "pending_verification" : "verified",
      pod_count: mapPods.length,
      listener_count: mapListeners.length,
    },
    pods: mapPods,
    listeners: mapListeners,
  };
}

const UNPLACED_DEPLOYMENT: MapDeployment = {
  id: UNPLACED_DEPLOYMENT_ID,
  name: "Unplaced Ridge",
  slug: "unplaced-ridge",
  gps_lat: null,
  gps_lon: null,
  status: "unknown",
  counts: counts([]),
  alert_count: 0,
  services_status: "unconfigured",
  pod_count: 0,
  listener_count: 0,
};

export function mapDeploymentsFixture(): MapDeployment[] {
  return [...deployments.map((row) => detailFor(row.id)!.deployment), UNPLACED_DEPLOYMENT];
}

export function mapDeploymentFixture(id: string): MapDeploymentDetail | null {
  if (id === UNPLACED_DEPLOYMENT_ID) {
    return { deployment: UNPLACED_DEPLOYMENT, pods: [], listeners: [] };
  }
  return detailFor(id);
}

// --- E6.5: `GET /map/devices/{kind}/{id}`, from the same derivation --------

/** The alerting Listener's liveness: it promised a wake and went quiet. */
export const SLEEPING_LIVENESS = {
  state: "sleeping" as const,
  last_audio_at: "2026-11-14T11:58:04Z",
  expected_wake_at: "2026-11-14T12:05:00Z",
  changed_at: "2026-11-14T11:58:30Z",
  reported_at: "2026-11-14T12:00:00Z",
  grace: { seconds: 30, source: "deployment", source_entity_id: FIXTURE_IDS.redwoodCoast },
};

function aggregatorOf(podId: string): MapAggregator | null {
  const agg = aggregators.find((row) => row.pod_id === podId);
  if (!agg) return null;
  const offline = agg.aggregator_uuid === OFFLINE_AGG;
  return {
    id: agg.id,
    name: null,
    aggregator_uuid: agg.aggregator_uuid,
    status: offline ? "offline" : "unknown",
    online: offline ? false : null,
    online_changed_at: offline ? "2026-11-14T03:00:00Z" : null,
    revision_state: offline ? "applied" : null,
  };
}

export function mapDeviceFixture(kind: string, rawId: string): MapDevice | null {
  const id = kind === "listener" ? rawId.toUpperCase() : rawId;
  const base = {
    revision_state: null,
    liveness: null,
    aggregator: null,
    counts: null,
    listener_count: null,
    pod_count: null,
    services_status: null,
  };
  if (kind === "deployment") {
    const detail = detailFor(id);
    if (!detail) return null;
    const { deployment } = detail;
    return {
      ...base,
      entity_type: "deployment",
      id,
      name: deployment.name,
      status: deployment.status,
      deployment_id: id,
      deployment_name: deployment.name,
      pod_id: null,
      pod_name: null,
      gps_lat: deployment.gps_lat,
      gps_lon: deployment.gps_lon,
      alerts: [],
      counts: deployment.counts,
      listener_count: deployment.listener_count,
      pod_count: deployment.pod_count,
      services_status: deployment.services_status,
    };
  }
  for (const deployment of deployments) {
    const detail = detailFor(deployment.id)!;
    if (kind === "pod" || kind === "aggregator") {
      const pod =
        kind === "pod"
          ? detail.pods.find((row) => row.id === id)
          : detail.pods.find((row) => row.aggregator_id === id);
      if (!pod) continue;
      const own = aggregatorOf(pod.id)!;
      const common = {
        ...base,
        deployment_id: deployment.id,
        deployment_name: deployment.name,
        pod_id: pod.id,
        pod_name: pod.name,
        gps_lat: pod.gps_lat,
        gps_lon: pod.gps_lon,
        alerts: [],
        aggregator: own,
      };
      return kind === "pod"
        ? {
            ...common,
            entity_type: "pod",
            id,
            name: pod.name,
            status: pod.status,
            counts: pod.counts,
            listener_count: pod.listener_count,
          }
        : {
            ...common,
            entity_type: "aggregator",
            id,
            name: own.aggregator_uuid,
            status: own.status,
            revision_state: own.revision_state,
          };
    }
    const listener = detail.listeners.find((row) => row.mac === id);
    if (!listener) continue;
    const pod = detail.pods.find((row) => row.id === listener.pod_id)!;
    const alerting = listener.mac === ALERTING_MAC;
    return {
      ...base,
      entity_type: "listener",
      id,
      name: listener.name,
      status: listener.status,
      deployment_id: deployment.id,
      deployment_name: deployment.name,
      pod_id: pod.id,
      pod_name: pod.name,
      gps_lat: listener.gps_lat,
      gps_lon: listener.gps_lon,
      alerts: alerting
        ? [
            {
              id: "a1000000-0000-4000-8000-000000000001",
              alert_type: "duplicate_identity",
              entity_type: "listener",
              entity_key: listener.mac,
              detail: { reported_by: "demo-agg-rc-02" },
              created_at: "2026-11-14T09:12:00Z",
            },
          ]
        : [],
      revision_state: alerting ? "applied" : null,
      liveness: alerting
        ? SLEEPING_LIVENESS
        : {
            state: null,
            last_audio_at: null,
            expected_wake_at: null,
            changed_at: null,
            reported_at: null,
            grace: { seconds: 45, source: "default", source_entity_id: null },
          },
      aggregator: aggregatorOf(pod.id),
    };
  }
  return null;
}
