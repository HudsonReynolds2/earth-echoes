/**
 * The line from the selected marker to the drawer's edge (task E6.5; V2·S2
 * fix 02: "a leader line runs from the marker to the drawer edge").
 *
 * Drawn in screen space over the map, from the marker element's center to the
 * drawer's left edge, and re-measured whenever the map moves, zooms or
 * resizes, or a marker is redrawn. **Hidden whenever the marker is not in
 * view**: scrolled off, folded into a cluster, or not on this level at all. A
 * line pointing at nothing would be worse than none.
 *
 * Markers are found by `data-marker-key` (set by `MarkerLayer`), the same key
 * the layer diffs by, so a live refetch that updates a marker in place keeps
 * the line attached.
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useMap } from "react-leaflet";

interface Segment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** Where the line ends on the drawer: never in its header or footer corners. */
const EDGE_INSET = 32;

export function findMarker(root: ParentNode, key: string): HTMLElement | null {
  for (const element of root.querySelectorAll<HTMLElement>("[data-marker-key]")) {
    if (element.dataset.markerKey === key) return element;
  }
  return null;
}

/** Pure, so it is testable without layout: the segment, or null when the
 * marker is outside the visible map or the drawer is not there. */
export function leaderSegment(
  view: DOMRect,
  map: DOMRect,
  marker: DOMRect,
  drawer: DOMRect,
): Segment | null {
  if (marker.width === 0 && marker.height === 0) return null;
  const x = marker.left + marker.width / 2;
  const y = marker.top + marker.height / 2;
  const visible = x >= map.left && x <= drawer.left && y >= map.top && y <= map.bottom;
  if (!visible) return null;
  const top = drawer.top + EDGE_INSET;
  const bottom = Math.max(top, drawer.bottom - EDGE_INSET);
  return {
    x1: x - view.left,
    y1: y - view.top,
    x2: drawer.left - view.left,
    y2: Math.min(Math.max(y, top), bottom) - view.top,
  };
}

export function LeaderLine({ markerKey }: { markerKey: string | null }) {
  const map = useMap();
  const [segment, setSegment] = useState<Segment | null>(null);
  const container = map.getContainer();
  const view = container.parentElement;

  useEffect(() => {
    if (markerKey === null || view === null) {
      setSegment(null);
      return;
    }
    const update = () => {
      const marker = findMarker(container, markerKey);
      const drawer = view.querySelector<HTMLElement>(".device-drawer");
      setSegment(
        marker && drawer
          ? leaderSegment(
              view.getBoundingClientRect(),
              container.getBoundingClientRect(),
              marker.getBoundingClientRect(),
              drawer.getBoundingClientRect(),
            )
          : null,
      );
    };
    update();
    map.on("move zoom moveend zoomend resize", update);
    // A marker added, re-iconed or swallowed by a cluster changes the DOM,
    // not the view; watch the panes too.
    const mutations = new MutationObserver(update);
    mutations.observe(container, { childList: true, subtree: true });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    resize?.observe(view);
    return () => {
      map.off("move zoom moveend zoomend resize", update);
      mutations.disconnect();
      resize?.disconnect();
    };
  }, [map, markerKey, container, view]);

  if (segment === null || view === null) return null;
  return createPortal(
    <svg className="map-leader" aria-hidden="true" data-testid="map-leader">
      <line x1={segment.x1} y1={segment.y1} x2={segment.x2} y2={segment.y2} />
      <circle cx={segment.x2} cy={segment.y2} r={4} />
    </svg>,
    view,
  );
}
