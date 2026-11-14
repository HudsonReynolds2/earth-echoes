/**
 * The map's tile-source interface (task E6.1; spec 9.1, 14.5, 15.1; D188).
 *
 * **One closed set of sources, one factory per source.** Spec 14.5 puts the
 * map tile source "behind an interface so [it swaps] without touching
 * callers". The map component calls `createBasemapLayer` and never names a
 * tile server. Adding a source means adding an id here, a definition, a
 * factory, and the matching id in `backend/app/map/basemaps.py`.
 * `basemaps.test.ts` and `test_map_config.py` pin both sides of that.
 *
 * **The server decides which sources are offered** (`GET /map/config`), and
 * supplies the operator-specific parts: a PMTiles URL, an image and its
 * bounds, a Google key. **This module owns everything else:** tile URL
 * templates, attribution and zoom limits. Attribution therefore lives next to
 * the code that draws it, never as a literal in a component.
 *
 * **Offline sources load their libraries lazily.** `pmtiles` and
 * `protomaps-leaflet` are dynamic imports, so a deploy that never configures
 * a PMTiles file never downloads them.
 */
import L from "leaflet";

export const BASEMAP_IDS = [
  "esri_imagery",
  "osm",
  "google_satellite",
  "pmtiles",
  "image",
  "none",
] as const;

export type BasemapId = (typeof BASEMAP_IDS)[number];

/** One offered source, as `GET /map/config` describes it. */
export interface BasemapEntry {
  id: BasemapId;
  url: string | null;
  /** [south, west, north, east], for `image` only. */
  bounds: [number, number, number, number] | null;
  /** The Google Maps Platform BROWSER key (D188: public by design,
   * referrer-restricted in the Google console). */
  api_key: string | null;
}

export interface MapConfig {
  offline: boolean;
  default: BasemapId;
  sources: BasemapEntry[];
}

interface BasemapDefinition {
  label: string;
  /** Fetches from the internet. Offline mode never offers these. */
  online: boolean;
  /** The host a CSP must allow for this source (E8, INTERFACES "Owned by
   * E6"). Null for sources whose origin is the operator's own URL. */
  host: string | null;
}

export const BASEMAPS: Record<BasemapId, BasemapDefinition> = {
  esri_imagery: { label: "Satellite (Esri)", online: true, host: "server.arcgisonline.com" },
  osm: { label: "Street (OpenStreetMap)", online: true, host: "tile.openstreetmap.org" },
  google_satellite: { label: "Satellite (Google)", online: true, host: "tile.googleapis.com" },
  pmtiles: { label: "Offline map", online: false, host: null },
  image: { label: "Site image", online: false, host: null },
  none: { label: "No basemap", online: false, host: null },
};

const ESRI_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const ESRI_ATTRIBUTION =
  'Powered by <a href="https://www.esri.com">Esri</a> · Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community';

const OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const GOOGLE_API = "https://tile.googleapis.com";
/** Google's policy allows text in place of the logo where space is tight;
 * the per-viewport copyright follows it (Map Tiles API policies). */
const GOOGLE_PREFIX = "Google Maps";

type LayerFactory = (entry: BasemapEntry) => Promise<L.Layer | null>;

/**
 * Exactly one factory per id. `Record<BasemapId, ...>` makes a missing
 * factory a type error, and `basemaps.test.ts` checks it at runtime too.
 * `none` returns null: no layer at all, just the map's own background.
 */
export const LAYER_FACTORIES: Record<BasemapId, LayerFactory> = {
  esri_imagery: async () =>
    L.tileLayer(ESRI_URL, { maxZoom: 19, maxNativeZoom: 19, attribution: ESRI_ATTRIBUTION }),
  osm: async () => L.tileLayer(OSM_URL, { maxZoom: 19, attribution: OSM_ATTRIBUTION }),
  google_satellite: async (entry) => googleSatelliteLayer(requireField(entry, "api_key")),
  pmtiles: async (entry) => pmtilesLayer(requireField(entry, "url")),
  image: async (entry) => {
    const url = requireField(entry, "url");
    const bounds = requireField(entry, "bounds");
    const [south, west, north, east] = bounds;
    return L.imageOverlay(url, [
      [south, west],
      [north, east],
    ]);
  },
  none: async () => null,
};

function requireField<K extends "url" | "bounds" | "api_key">(
  entry: BasemapEntry,
  key: K,
): NonNullable<BasemapEntry[K]> {
  const value = entry[key];
  if (value === null || value === undefined) {
    throw new Error(`basemap ${entry.id} is missing ${key}`);
  }
  return value as NonNullable<BasemapEntry[K]>;
}

export function createBasemapLayer(entry: BasemapEntry): Promise<L.Layer | null> {
  return LAYER_FACTORIES[entry.id](entry);
}

// --- Google Map Tiles API -------------------------------------------------------

interface GoogleSession {
  session: string;
}

interface GoogleViewport {
  copyright?: string;
}

/**
 * Google 2D satellite tiles in Leaflet, the way the Map Tiles API policies
 * permit a third-party renderer: a session token, the "Google Maps" mark,
 * and the copyright for the CURRENT viewport, refreshed as the map moves.
 * Tiles are not cached by this code. The browser honors the response's
 * cache headers, as the policy requires.
 */
async function googleSatelliteLayer(key: string): Promise<L.Layer> {
  const response = await fetch(`${GOOGLE_API}/v1/createSession?key=${encodeURIComponent(key)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mapType: "satellite", language: "en-US", region: "US" }),
  });
  if (!response.ok) {
    throw new Error(`Google Map Tiles session failed: ${response.status}`);
  }
  const { session } = (await response.json()) as GoogleSession;
  const query = `session=${encodeURIComponent(session)}&key=${encodeURIComponent(key)}`;
  const layer = L.tileLayer(`${GOOGLE_API}/v1/2dtiles/{z}/{x}/{y}?${query}`, {
    maxZoom: 22,
    attribution: GOOGLE_PREFIX,
  });

  let current = GOOGLE_PREFIX;
  let map: L.Map | null = null;
  const refresh = async () => {
    if (map === null) return;
    const bounds = map.getBounds();
    const url =
      `${GOOGLE_API}/tile/v1/viewport?${query}&zoom=${map.getZoom()}` +
      `&north=${bounds.getNorth()}&south=${bounds.getSouth()}` +
      `&east=${bounds.getEast()}&west=${bounds.getWest()}`;
    try {
      const viewport = (await (await fetch(url)).json()) as GoogleViewport;
      const next = viewport.copyright ? `${GOOGLE_PREFIX} · ${viewport.copyright}` : GOOGLE_PREFIX;
      if (map !== null && next !== current) {
        map.attributionControl?.removeAttribution(current).addAttribution(next);
        current = next;
      }
    } catch {
      // Keep the last attribution; the next move retries.
    }
  };
  layer.on("add", () => {
    map = (layer as unknown as { _map: L.Map })._map;
    map.on("moveend", refresh);
    void refresh();
  });
  layer.on("remove", () => {
    if (map !== null) {
      map.off("moveend", refresh);
      map.attributionControl?.removeAttribution(current);
    }
    map = null;
    current = GOOGLE_PREFIX;
  });
  return layer;
}

// --- PMTiles (spec 9.1's offline basemap) --------------------------------------------

/**
 * A raster archive (offline satellite imagery) draws as raster tiles. A
 * vector archive (an offline street map) is painted by protomaps-leaflet. The
 * archive's own header says which it is, so the operator configures one URL.
 */
async function pmtilesLayer(url: string): Promise<L.Layer> {
  const { PMTiles, TileType, leafletRasterLayer } = await import("pmtiles");
  const archive = new PMTiles(url);
  const header = await archive.getHeader();
  if (header.tileType === TileType.Mvt) {
    const { leafletLayer } = await import("protomaps-leaflet");
    // protomaps-leaflet bundles its own pmtiles copy, so it is handed the URL
    // rather than this module's archive object; it reads the header itself.
    return leafletLayer({ url, flavor: "light", attribution: "Offline map" }) as unknown as L.Layer;
  }
  return leafletRasterLayer(archive, { attribution: "Offline map" }) as L.Layer;
}

// --- Choosing and remembering ----------------------------------------------------------

const STORAGE_KEY = "eoe.map.basemap";

/** The viewer's last choice if it is still offered, else the server default.
 * A remembered online source on a host that went offline falls back cleanly. */
export function chooseBasemap(config: MapConfig, remembered: string | null): BasemapId {
  const offered = config.sources.map((source) => source.id);
  if (remembered !== null && (offered as string[]).includes(remembered)) {
    return remembered as BasemapId;
  }
  return config.default;
}

/** Per-viewer convenience only. Storage can be absent or throw (private
 * windows, blocked site data), and the map must work without it. */
export function recallBasemap(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function rememberBasemap(id: BasemapId): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Not remembered; the default applies next time.
  }
}
