/**
 * The map's API client (epic E6). Shaped after `lib/inventory.ts`: the typed
 * `ApiError` from `lib/http.ts`, one function per call, and flat query keys,
 * all under `["map", ...]` so the live-update hook (E6.6) can invalidate the
 * whole map with one key.
 */
import type { MapConfig } from "./basemaps";
import { request } from "./http";

export { ApiError } from "./http";

export const mapKeys = {
  config: ["map", "config"] as const,
};

export function getMapConfig(): Promise<MapConfig> {
  return request<MapConfig>("/map/config");
}
