/**
 * E6.1: the tile-source interface (spec 9.1, 14.5; D188).
 *
 * The id set is closed and mirrored from `backend/app/map/basemaps.py`
 * (`test_map_config.py` pins the same list), every id has exactly one layer
 * factory, and attribution comes from the source definition. No test here
 * touches a tile server: the factories build layer OBJECTS, which fetch
 * nothing until they are added to a sized map, and Google's two API calls
 * go to MSW.
 */
import L from "leaflet";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BASEMAPS,
  BASEMAP_IDS,
  BasemapEntry,
  LAYER_FACTORIES,
  MapConfig,
  chooseBasemap,
  createBasemapLayer,
  recallBasemap,
  rememberBasemap,
} from "../src/lib/basemaps";
import { server } from "./msw-server";

const entry = (id: BasemapEntry["id"], extra: Partial<BasemapEntry> = {}): BasemapEntry => ({
  id,
  url: null,
  bounds: null,
  api_key: null,
  ...extra,
});

const config = (ids: BasemapEntry["id"][], fallback: BasemapEntry["id"]): MapConfig => ({
  offline: false,
  default: fallback,
  sources: ids.map((id) => entry(id)),
});

function attributionOf(layer: L.Layer | null): string {
  return (layer as L.TileLayer).getAttribution?.() ?? "";
}

describe("the closed source set", () => {
  it("matches the backend's list, in switcher order", () => {
    expect(BASEMAP_IDS).toEqual([
      "esri_imagery",
      "osm",
      "google_satellite",
      "pmtiles",
      "image",
      "none",
    ]);
  });

  it("gives every id a definition and exactly one factory", () => {
    expect(Object.keys(LAYER_FACTORIES).sort()).toEqual([...BASEMAP_IDS].sort());
    expect(Object.keys(BASEMAPS).sort()).toEqual([...BASEMAP_IDS].sort());
  });

  it("marks exactly the internet sources as online, each with the host a CSP must allow", () => {
    const online = BASEMAP_IDS.filter((id) => BASEMAPS[id].online);
    expect(online).toEqual(["esri_imagery", "osm", "google_satellite"]);
    for (const id of online) expect(BASEMAPS[id].host).toMatch(/\./);
  });
});

describe("layer factories", () => {
  it("builds Esri satellite with Esri's attribution", async () => {
    const layer = await createBasemapLayer(entry("esri_imagery"));
    expect(layer).toBeInstanceOf(L.TileLayer);
    expect((layer as unknown as { _url: string })._url).toContain("World_Imagery");
    expect(attributionOf(layer)).toContain("Esri");
  });

  it("builds OpenStreetMap with the contributors credit", async () => {
    const layer = await createBasemapLayer(entry("osm"));
    expect(attributionOf(layer)).toContain("OpenStreetMap");
  });

  it("draws nothing for `none`", async () => {
    expect(await createBasemapLayer(entry("none"))).toBeNull();
  });

  it("places an operator image at its configured bounds", async () => {
    const layer = (await createBasemapLayer(
      entry("image", { url: "/site.png", bounds: [41, -124.2, 41.4, -123.8] }),
    )) as L.ImageOverlay;
    expect(layer).toBeInstanceOf(L.ImageOverlay);
    const bounds = layer.getBounds();
    expect([bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()]).toEqual([
      41, -124.2, 41.4, -123.8,
    ]);
  });

  it("refuses a source the server sent without its parameters", async () => {
    await expect(createBasemapLayer(entry("image"))).rejects.toThrow("missing url");
    await expect(createBasemapLayer(entry("google_satellite"))).rejects.toThrow("missing api_key");
  });

  it("opens a Google session and builds tiles on it, without exposing more than the key", async () => {
    let body: unknown = null;
    server.use(
      http.post("https://tile.googleapis.com/v1/createSession", async ({ request }) => {
        body = await request.json();
        expect(new URL(request.url).searchParams.get("key")).toBe("browser-key");
        return HttpResponse.json({ session: "sess-1", expiry: "0", tileWidth: 256 });
      }),
    );
    const layer = await createBasemapLayer(entry("google_satellite", { api_key: "browser-key" }));
    expect(body).toMatchObject({ mapType: "satellite" });
    const url = (layer as unknown as { _url: string })._url;
    expect(url).toContain("/v1/2dtiles/{z}/{x}/{y}");
    expect(url).toContain("session=sess-1");
    expect(attributionOf(layer)).toContain("Google Maps");
  });

  it("surfaces a failed Google session as an error the map can show", async () => {
    server.use(
      http.post("https://tile.googleapis.com/v1/createSession", () =>
        HttpResponse.json({ error: "denied" }, { status: 403 }),
      ),
    );
    await expect(createBasemapLayer(entry("google_satellite", { api_key: "bad" }))).rejects.toThrow(
      "403",
    );
  });
});

describe("choosing and remembering", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("uses the remembered choice while it is still offered", () => {
    expect(chooseBasemap(config(["esri_imagery", "osm", "none"], "esri_imagery"), "osm")).toBe(
      "osm",
    );
  });

  it("falls back to the server default when the remembered source is gone", () => {
    // A host that went offline, or a Google key that was removed.
    expect(chooseBasemap(config(["pmtiles", "none"], "pmtiles"), "esri_imagery")).toBe("pmtiles");
    expect(chooseBasemap(config(["esri_imagery", "none"], "esri_imagery"), null)).toBe(
      "esri_imagery",
    );
  });

  it("round-trips through storage", () => {
    rememberBasemap("osm");
    expect(recallBasemap()).toBe("osm");
  });

  it("works when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => rememberBasemap("osm")).not.toThrow();
    expect(recallBasemap()).toBeNull();
  });
});
