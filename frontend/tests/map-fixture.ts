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
  MapDeployment,
  MapDeploymentDetail,
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
