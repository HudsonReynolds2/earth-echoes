/**
 * The map engine (task E6.1; spec 9.1; D188): Leaflet through react-leaflet,
 * with the basemap behind `lib/basemaps.ts`'s interface.
 *
 * This component knows nothing about tile servers. It asks the factory for a
 * layer and draws whatever comes back. Markers arrive with E6.2b as children.
 */
import "leaflet/dist/leaflet.css";

import type L from "leaflet";
import { ReactNode, useEffect, useState } from "react";
import { MapContainer, useMap } from "react-leaflet";

import {
  BASEMAPS,
  BasemapId,
  MapConfig,
  chooseBasemap,
  createBasemapLayer,
  recallBasemap,
  rememberBasemap,
} from "../../lib/basemaps";

/** Where an empty map opens: the western United States, where the demo and
 * the first field sites are. E6.2b fits the view to placed markers instead. */
const INITIAL_CENTER: [number, number] = [42.5, -121.5];
const INITIAL_ZOOM = 6;

/** Leaflet's default prefix carries a flag graphic; the platform states the
 * library plainly and leaves the space for the basemap's own attribution. */
const ATTRIBUTION_PREFIX = '<a href="https://leafletjs.com">Leaflet</a>';

export function MapView({
  config,
  children,
  overlay,
}: {
  config: MapConfig;
  /** Leaflet layers (markers, from E6.2b). */
  children?: ReactNode;
  /** Ordinary DOM drawn over the map, outside Leaflet's panes. */
  overlay?: ReactNode;
}) {
  const [basemap, setBasemap] = useState<BasemapId>(() => chooseBasemap(config, recallBasemap()));

  const choose = (id: BasemapId) => {
    setBasemap(id);
    rememberBasemap(id);
  };

  return (
    <div className="map-view" data-testid="map-view" data-basemap={basemap}>
      <MapContainer
        center={INITIAL_CENTER}
        zoom={INITIAL_ZOOM}
        className="map-canvas"
        attributionControl
        zoomControl
      >
        <AttributionPrefix />
        <BasemapLayer config={config} id={basemap} />
        {children}
      </MapContainer>
      <BasemapSwitcher config={config} value={basemap} onChange={choose} />
      {overlay}
    </div>
  );
}

function AttributionPrefix() {
  const map = useMap();
  useEffect(() => {
    map.attributionControl?.setPrefix(ATTRIBUTION_PREFIX);
  }, [map]);
  return null;
}

/**
 * Swaps the basemap layer in place. Layer creation is async (Google needs a
 * session, PMTiles reads its header), so a slow factory that finishes after
 * the operator already chose something else is discarded rather than drawn.
 */
function BasemapLayer({ config, id }: { config: MapConfig; id: BasemapId }) {
  const map = useMap();
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    const entry = config.sources.find((source) => source.id === id);
    if (entry === undefined) return;
    let cancelled = false;
    let layer: L.Layer | null = null;
    setFailed(null);
    createBasemapLayer(entry)
      .then((created) => {
        if (cancelled || created === null) return;
        layer = created;
        created.addTo(map);
        // Behind every marker, always.
        if ("bringToBack" in created && typeof created.bringToBack === "function") {
          created.bringToBack();
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) setFailed(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
      if (layer !== null) map.removeLayer(layer);
    };
  }, [config, id, map]);

  if (failed === null) return null;
  return (
    <div className="map-notice map-notice-error" role="status" data-testid="basemap-error">
      {BASEMAPS[id].label} could not load: {failed}. Choose another basemap.
    </div>
  );
}

function BasemapSwitcher({
  config,
  value,
  onChange,
}: {
  config: MapConfig;
  value: BasemapId;
  onChange: (id: BasemapId) => void;
}) {
  return (
    <label className="map-control basemap-switcher" data-testid="basemap-switcher">
      <span className="eyebrow">Basemap</span>
      <select value={value} onChange={(event) => onChange(event.target.value as BasemapId)}>
        {config.sources.map((source) => (
          <option key={source.id} value={source.id}>
            {BASEMAPS[source.id].label}
          </option>
        ))}
      </select>
    </label>
  );
}
