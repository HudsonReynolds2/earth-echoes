/**
 * GPS-less Listeners, fanned around their Pod on dashed tethers (task E6.3;
 * spec 9.2).
 *
 * Each leaf is placed by a PIXEL offset (`leafFan.ts`) projected at the
 * current zoom, and re-projected whenever the zoom changes. Its tether is a
 * CSS line inside the leaf's own markup, drawn back along that offset. That keeps leaves
 * a fixed distance from their Pod on screen, which is the visual promise
 * that they are not real coordinates. When a Listener gains GPS it simply
 * stops being a leaf: the next render draws it as a pin instead.
 */
import { useEffect, useMemo, useState } from "react";
import { useMap } from "react-leaflet";

import { MapListener, MapPod, isPlaced } from "../../lib/map";
import { leafOffsets } from "./leafFan";
import { MarkerLayer, MarkerSpec } from "./MarkerLayer";
import { LISTENER_SIZE, MapTab, accessibleName, isDimmed, listenerPinHtml } from "./markers";

/** On the Deployment view, leaves appear from this zoom in. Further out, a
 * fan of 50 per Pod would bury the Pods it is meant to explain. On a Pod's
 * own route, its leaves always show. */
export const LEAF_ZOOM = 14;

export function LeafFan({
  pods,
  listeners,
  focusPodId,
  tab,
  selected,
  onSelect,
}: {
  pods: MapPod[];
  listeners: MapListener[];
  focusPodId: string | undefined;
  tab: MapTab;
  selected: string | null;
  onSelect: (mac: string) => void;
}) {
  const map = useMap();
  const [zoom, setZoom] = useState(() => map.getZoom());

  useEffect(() => {
    const update = () => setZoom(map.getZoom());
    map.on("zoomend", update);
    return () => {
      map.off("zoomend", update);
    };
  }, [map]);

  const fans = useMemo(() => {
    const showAll = focusPodId === undefined && zoom >= LEAF_ZOOM;
    return pods
      .filter(isPlaced)
      .filter((pod) => showAll || pod.id === focusPodId)
      .map((pod) => {
        const leaves = listeners
          .filter((row) => row.pod_id === pod.id && !isPlaced(row))
          .sort((a, b) => a.name.localeCompare(b.name));
        const origin = map.project([pod.gps_lat, pod.gps_lon], zoom);
        const offsets = leafOffsets(leaves.length);
        return {
          pod,
          leaves: leaves.map((row, index) => {
            const point = map.unproject(origin.add([offsets[index].dx, offsets[index].dy]), zoom);
            return { row, lat: point.lat, lon: point.lng, offset: offsets[index] };
          }),
        };
      });
  }, [pods, listeners, focusPodId, zoom, map]);

  const specs: MarkerSpec[] = useMemo(
    () =>
      fans.flatMap(({ leaves }) =>
        leaves.map(({ row, lat, lon, offset }) => ({
          key: `listener:${row.mac}`,
          lat,
          lon,
          size: LISTENER_SIZE,
          status: row.status,
          label: accessibleName(
            "listener",
            row.name,
            row.status,
            row.alert_count,
            "no GPS, drawn beside its pod",
          ),
          html: listenerPinHtml({
            status: row.status,
            alerts: row.alert_count,
            dimmed: isDimmed(tab, row.status, row.alert_count),
            selected: selected === `listener:${row.mac}`,
            leaf: { dx: offset.dx, dy: offset.dy },
          }),
          onSelect: () => onSelect(row.mac),
        })),
      ),
    [fans, tab, selected, onSelect],
  );

  return <MarkerLayer specs={specs} cluster={false} />;
}
