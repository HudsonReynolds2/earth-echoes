/**
 * The map's API client (epic E6). Shaped after `lib/inventory.ts`: the typed
 * `ApiError` from `lib/http.ts`, one function per call, and flat query keys,
 * all under `["map", ...]` so the live-update hook (E6.6) can invalidate the
 * whole map with one key.
 */
import type { RolledUpStatus } from "../components/StatusChip";
import type { MapConfig } from "./basemaps";
import { request, writeHeaders } from "./http";

export { ApiError } from "./http";

export const mapKeys = {
  config: ["map", "config"] as const,
  deployments: ["map", "deployments"] as const,
  deployment: (id: string) => ["map", "deployment", id] as const,
  device: (kind: DeviceKind, id: string) => ["map", "device", kind, id] as const,
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

// --- E6.5: the device drawer ------------------------------------------------

export type DeviceKind = "listener" | "aggregator" | "pod" | "deployment";
const DEVICE_KINDS: readonly DeviceKind[] = ["listener", "aggregator", "pod", "deployment"];

/** What the drawer is open on: `?device=<kind>:<id>`. A Listener's id is its
 * MAC, which has colons of its own, so only the first colon separates. */
export interface DrawerTarget {
  kind: DeviceKind;
  id: string;
}

export function parseDrawerTarget(value: string | null): DrawerTarget | null {
  if (value === null) return null;
  const split = value.indexOf(":");
  if (split <= 0 || split === value.length - 1) return null;
  const kind = value.slice(0, split) as DeviceKind;
  if (!DEVICE_KINDS.includes(kind)) return null;
  return { kind, id: value.slice(split + 1) };
}

export function formatDrawerTarget(target: DrawerTarget): string {
  return `${target.kind}:${target.id}`;
}

/** The drawer's tabs, in order. `?panel=` names the open one. */
export const DRAWER_PANELS = ["status", "config", "timeline", "card", "telemetry"] as const;
export type DrawerPanel = (typeof DRAWER_PANELS)[number];

export function parseDrawerPanel(value: string | null): DrawerPanel {
  return DRAWER_PANELS.includes(value as DrawerPanel) ? (value as DrawerPanel) : "status";
}

export interface MapAlert {
  id: string;
  alert_type: string;
  entity_type: string;
  entity_key: string;
  detail: Record<string, unknown> | null;
  created_at: string;
}

export interface Liveness {
  /** Spec 6.5: `streaming`, `sleeping` or `offline`; null if never reported. */
  state: "streaming" | "sleeping" | "offline" | null;
  last_audio_at: string | null;
  expected_wake_at: string | null;
  changed_at: string | null;
  reported_at: string | null;
  grace: {
    seconds: number | null;
    source: string;
    source_entity_id: string | null;
  };
}

export interface MapAggregator {
  id: string;
  name: string | null;
  aggregator_uuid: string;
  status: RolledUpStatus;
  /** Null when no status message has ever arrived. */
  online: boolean | null;
  online_changed_at: string | null;
  revision_state: string | null;
}

/** `GET /map/devices/{kind}/{id}`. Sections that do not apply are null. */
export interface MapDevice {
  entity_type: DeviceKind;
  id: string;
  name: string;
  status: RolledUpStatus;
  deployment_id: string;
  deployment_name: string;
  pod_id: string | null;
  pod_name: string | null;
  gps_lat: number | null;
  gps_lon: number | null;
  alerts: MapAlert[];
  revision_state: string | null;
  liveness: Liveness | null;
  aggregator: MapAggregator | null;
  counts: StatusCounts | null;
  listener_count: number | null;
  pod_count: number | null;
  services_status: ServicesStatus | null;
}

export function getMapDevice(kind: DeviceKind, id: string): Promise<MapDevice> {
  return request<MapDevice>(`/map/devices/${kind}/${encodeURIComponent(id)}`);
}

/** E3.10's command channel, for the drawer's Resync (spec 7.2). 202 means
 * published, not done: the device's answer arrives as a timeline entry. */
export function sendAggregatorCommand(
  aggregatorId: string,
  command: "resync" | "restart" | "flush_buffer",
): Promise<{ command_id: string; published_at: string }> {
  return request(`/aggregators/${aggregatorId}/commands`, {
    method: "POST",
    headers: writeHeaders(),
    body: JSON.stringify({ command }),
  });
}
