/**
 * The map's API client (epic E6). Shaped after `lib/inventory.ts`: the typed
 * `ApiError` from `lib/http.ts`, one function per call, and flat query keys,
 * all under `["map", ...]` so the live-update hook (E6.6) can invalidate the
 * whole map with one key.
 */
import type { RolledUpStatus } from "../components/StatusChip";
import type { MapConfig } from "./basemaps";
import { request } from "./http";

export { ApiError } from "./http";

export const mapKeys = {
  config: ["map", "config"] as const,
  deployments: ["map", "deployments"] as const,
  deployment: (id: string) => ["map", "deployment", id] as const,
};

/** `deployment.services_status` (spec 16.5). Not a device status (D40). */
export type ServicesStatus = "unconfigured" | "pending_verification" | "verified" | "degraded";

/** Devices per status, every status present, in severity order (E6.4). */
export type StatusCounts = Record<RolledUpStatus, number>;

export interface MapDeployment {
  id: string;
  name: string;
  slug: string;
  gps_lat: number | null;
  gps_lon: number | null;
  status: RolledUpStatus;
  counts: StatusCounts;
  alert_count: number;
  services_status: ServicesStatus;
  pod_count: number;
  listener_count: number;
}

export interface MapPod {
  id: string;
  name: string;
  aggregator_id: string | null;
  aggregator_uuid: string | null;
  aggregator_name: string | null;
  /** The Aggregator's position, which is the Pod marker's (spec 9.2). */
  gps_lat: number | null;
  gps_lon: number | null;
  aggregator_status: RolledUpStatus | null;
  status: RolledUpStatus;
  counts: StatusCounts;
  alert_count: number;
  listener_count: number;
}

export interface MapListener {
  mac: string;
  name: string;
  pod_id: string;
  aggregator_id: string;
  gps_lat: number | null;
  gps_lon: number | null;
  status: RolledUpStatus;
  alert_count: number;
}

export interface MapDeploymentDetail {
  deployment: MapDeployment;
  pods: MapPod[];
  listeners: MapListener[];
}

export function getMapConfig(): Promise<MapConfig> {
  return request<MapConfig>("/map/config");
}

export function getMapDeployments(): Promise<MapDeployment[]> {
  return request<MapDeployment[]>("/map/deployments");
}

export function getMapDeployment(id: string): Promise<MapDeploymentDetail> {
  return request<MapDeploymentDetail>(`/map/deployments/${id}`);
}

/** Placed means both coordinates. The API guarantees both-or-neither for
 * Deployments and Aggregators (D189); Listeners are checked the same way. */
export function isPlaced<T extends { gps_lat: number | null; gps_lon: number | null }>(
  row: T,
): row is T & { gps_lat: number; gps_lon: number } {
  return row.gps_lat !== null && row.gps_lon !== null;
}
