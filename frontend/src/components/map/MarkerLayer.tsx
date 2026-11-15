/**
 * Draws a set of markers and keeps them in step with the data (task E6.2b).
 *
 * **Keyed, and updated in place.** Every marker is keyed by its entity
 * (`deployment:<id>`, `pod:<id>`, `listener:<mac>`). A new render moves,
 * re-icons, adds or removes only what changed, so a refetch never rebuilds the
 * map. That is what lets E6.6's live updates arrive without the operator's
 * zoom, open clusters, or keyboard focus moving.
 *
 * Imperative on purpose. react-leaflet's declarative `<Marker>` would remount
 * on key changes and cannot host `leaflet.markercluster`'s group directly; one
 * `useEffect` diffing a `Map` is smaller and is the whole contract.
 */
// Order matters: the global must exist before the UMD plugin evaluates.
import L from "./leafletGlobal";
import "leaflet.markercluster";
import "leaflet.markercluster/dist/MarkerCluster.css";

import { useEffect, useRef } from "react";
import { useMap } from "react-leaflet";

import type { RolledUpStatus } from "../StatusChip";
import { CLUSTER_SIZE, clusterHtml } from "./markers";

export interface MarkerSpec {
  key: string;
  lat: number;
  lon: number;
  html: string;
  /** The shape's box in px; the clickable, focusable element is this size. */
  size: number;
  /** The accessible name a screen reader announces on focus. */
  label: string;
  status: RolledUpStatus;
  onSelect: () => void;
}

interface StatusMarkerOptions extends L.MarkerOptions {
  status: RolledUpStatus;
}

function iconFor(html: string, size: number): L.DivIcon {
  // Leaflet sizes the element to the shape and centers it on the coordinate,
  // so the click and focus target is exactly the shape the operator sees.
  return L.divIcon({ html, className: "map-marker-anchor", iconSize: [size, size] });
}

function label(marker: L.Marker, text: string) {
  const element = marker.getElement();
  if (element) {
    element.setAttribute("aria-label", text);
    element.setAttribute("role", "button");
  }
}

export function MarkerLayer({ specs, cluster }: { specs: MarkerSpec[]; cluster: boolean }) {
  const map = useMap();
  const groupRef = useRef<L.LayerGroup | null>(null);
  const markersRef = useRef(new Map<string, { marker: L.Marker; spec: MarkerSpec }>());
  const selectRef = useRef(new Map<string, () => void>());

  useEffect(() => {
    const group: L.LayerGroup = cluster
      ? L.markerClusterGroup({
          showCoverageOnHover: false,
          // Spec 4.1: a Pod's ~50 Listeners sit within a few hundred meters,
          // so only at close zoom do they separate.
          maxClusterRadius: 48,
          iconCreateFunction: (group) =>
            L.divIcon({
              html: clusterHtml(
                group
                  .getAllChildMarkers()
                  .map((child) => (child.options as StatusMarkerOptions).status),
              ),
              className: "map-marker-anchor",
              iconSize: [CLUSTER_SIZE, CLUSTER_SIZE],
            }),
        })
      : L.layerGroup();
    group.addTo(map);
    groupRef.current = group;
    const markers = markersRef.current;
    return () => {
      group.remove();
      groupRef.current = null;
      markers.clear();
    };
  }, [map, cluster]);

  useEffect(() => {
    const group = groupRef.current;
    if (group === null) return;
    const markers = markersRef.current;
    const wanted = new Set(specs.map((spec) => spec.key));
    let statusMoved = false;

    for (const [key, entry] of markers) {
      if (!wanted.has(key)) {
        group.removeLayer(entry.marker);
        markers.delete(key);
        selectRef.current.delete(key);
      }
    }

    for (const spec of specs) {
      selectRef.current.set(spec.key, spec.onSelect);
      const existing = markers.get(spec.key);
      if (existing === undefined) {
        const marker = L.marker([spec.lat, spec.lon], {
          icon: iconFor(spec.html, spec.size),
          keyboard: true,
          title: spec.label,
          status: spec.status,
        } as StatusMarkerOptions);
        const select = () => selectRef.current.get(spec.key)?.();
        marker.on("click", select);
        // A clustered marker gets a fresh element each time its cluster opens,
        // so naming and the keyboard handler are attached on every add, once
        // per element.
        marker.on("add", () => {
          label(marker, markers.get(spec.key)?.spec.label ?? spec.label);
          const element = marker.getElement();
          if (element && element.dataset.keyed !== "true") {
            element.dataset.keyed = "true";
            element.addEventListener("keydown", (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                select();
              }
            });
          }
        });
        group.addLayer(marker);
        markers.set(spec.key, { marker, spec });
        continue;
      }
      const { marker, spec: previous } = existing;
      if (previous.lat !== spec.lat || previous.lon !== spec.lon) {
        marker.setLatLng([spec.lat, spec.lon]);
      }
      if (previous.html !== spec.html || previous.size !== spec.size) {
        marker.setIcon(iconFor(spec.html, spec.size));
      }
      if (previous.label !== spec.label || previous.html !== spec.html) {
        marker.options.title = spec.label;
        label(marker, spec.label);
      }
      if (previous.status !== spec.status) {
        (marker.options as StatusMarkerOptions).status = spec.status;
        statusMoved = true;
      }
      markers.set(spec.key, { marker, spec });
    }

    // A cluster's icon is the worst status inside it, so a status change
    // re-renders the clusters it touched.
    if (statusMoved && "refreshClusters" in group) {
      (group as L.MarkerClusterGroup).refreshClusters();
    }
  }, [specs]);

  return null;
}

/** Fit the view to a level's points once per level, never on a refetch, so
 * a live update does not pull the operator's view away (E6.6). */
export function FitToPoints({ fitKey, points }: { fitKey: string; points: [number, number][] }) {
  const map = useMap();
  const fittedRef = useRef<string | null>(null);

  useEffect(() => {
    if (fittedRef.current === fitKey || points.length === 0) return;
    fittedRef.current = fitKey;
    const size = map.getSize();
    if (points.length === 1 || size.x === 0 || size.y === 0) {
      // One point has no extent, and a map with no size (jsdom, a hidden tab)
      // cannot compute a zoom that fits anything.
      map.setView(points[0], points.length === 1 ? 15 : 10);
      return;
    }
    map.fitBounds(L.latLngBounds(points), { padding: [64, 64], maxZoom: 16 });
  }, [fitKey, points, map]);

  return null;
}
