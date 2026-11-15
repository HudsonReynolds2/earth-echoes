/**
 * Map surface (spec §9). DES.7 built the frame: context band, view tabs and
 * the status legend. E6 fills it.
 *
 * - E6.1: Leaflet with switchable basemaps, satellite by default (D188).
 * - E6.2b: three drill levels, each its own route so back, forward and deep
 *   links work.
 *   - `/map` shows one marker per Deployment.
 *   - `/map/deployments/:id` shows its Pods as squares and its Listeners as
 *     clustered pins.
 *   - `/map/pods/:id` is the same Deployment, zoomed to one Pod.
 *
 * Every status comes from the API's rollup (E6.4). The only one computed here
 * is a cluster's icon, with the server's severity order.
 *
 * - E6.5: `?device=<kind>:<id>` opens the device drawer on any level, and
 *   `?panel=` names its tab. Both live in the URL, so a link reopens the same
 *   drawer and a live refetch (E6.6) cannot close it.
 */
import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ContextBar, Crumb } from "../components/ContextBar";
import { EmptyState } from "../components/EmptyState";
import {
  DEPLOYMENT_SIZE,
  LISTENER_SIZE,
  MapTab,
  accessibleName,
  deploymentMarkerHtml,
  isDimmed,
  listenerPinHtml,
  podMarkerHtml,
  podMarkerSize,
} from "../components/map/markers";
import { DeviceDrawer } from "../components/map/DeviceDrawer";
import { LeafFan } from "../components/map/LeafFan";
import { LeaderLine, findMarker } from "../components/map/LeaderLine";
import { FitToPoints, MarkerLayer, MarkerSpec } from "../components/map/MarkerLayer";
import { MapView } from "../components/map/MapView";
import { UnplacedItem, UnplacedPanel } from "../components/map/UnplacedPanel";
import { StatusLegend } from "../components/StatusChip";
import { getPod } from "../lib/inventory";
import {
  DrawerPanel,
  DrawerTarget,
  MapDeployment,
  MAP_POLL_MS,
  MapDeploymentDetail,
  formatDrawerTarget,
  getMapConfig,
  getMapDeployment,
  getMapDeployments,
  getMapDevice,
  isPlaced,
  mapKeys,
  parseDrawerPanel,
  parseDrawerTarget,
} from "../lib/map";

const TABS: MapTab[] = ["Status", "Reconciliation", "Alerts"];

function tabFromUrl(value: string | null): MapTab {
  return TABS.find((name) => name.toLowerCase() === value?.toLowerCase()) ?? "Status";
}

const LEAVES_KEY = "eoe.map.showLeaves";

/** Per-viewer convenience: on unless this viewer turned it off. Storage can
 * be absent or throw, and the map must work without it. */
function recallShowLeaves(): boolean {
  try {
    return window.localStorage.getItem(LEAVES_KEY) !== "off";
  } catch {
    return true;
  }
}

function rememberShowLeaves(on: boolean): void {
  try {
    window.localStorage.setItem(LEAVES_KEY, on ? "on" : "off");
  } catch {
    // Not remembered; leaves show by default next time.
  }
}

/** A MAC as the API spells it, so a link with `aa-bb-…` still finds the
 * marker keyed `listener:AA:BB:…`. Anything else is left alone. */
function canonicalMac(value: string): string {
  const hex = value.replace(/[^0-9a-fA-F]/g, "");
  return hex.length === 12 ? hex.toUpperCase().match(/../g)!.join(":") : value;
}

export interface Drawer {
  target: DrawerTarget | null;
  panel: DrawerPanel;
  /** The marker the drawer belongs to: its halo, line and focus return. */
  markerKey: string | null;
  open: (target: DrawerTarget) => void;
  close: () => void;
  setPanel: (panel: DrawerPanel) => void;
}

function useDrawer(): Drawer {
  const [params, setParams] = useSearchParams();
  const raw = parseDrawerTarget(params.get("device"));
  const target = useMemo(
    () => (raw && raw.kind === "listener" ? { ...raw, id: canonicalMac(raw.id) } : raw),
    // Keyed by value: a new URLSearchParams each render must not mean a new target.
    [raw?.kind, raw?.id],
  );
  const panel = parseDrawerPanel(params.get("panel"));
  // Shares the drawer's own query: an Aggregator's marker is its Pod's.
  const device = useQuery({
    queryKey: mapKeys.device(target?.kind ?? "listener", target?.id ?? ""),
    queryFn: () => getMapDevice(target!.kind, target!.id),
    enabled: target?.kind === "aggregator",
  });
  const markerKey =
    target === null
      ? null
      : target.kind === "aggregator"
        ? device.data?.pod_id
          ? `pod:${device.data.pod_id}`
          : null
        : formatDrawerTarget(target);

  const open = useCallback(
    (next: DrawerTarget) => {
      const updated = new URLSearchParams(params);
      updated.set("device", formatDrawerTarget(next));
      updated.delete("panel");
      setParams(updated);
    },
    [params, setParams],
  );
  const close = useCallback(() => {
    // Focus returns to the marker the drawer belonged to, or to the map if
    // that marker is folded into a cluster or not on this level.
    const map = document.querySelector<HTMLElement>(".leaflet-container");
    const marker = markerKey && map ? findMarker(map, markerKey) : null;
    (marker ?? map)?.focus();
    const updated = new URLSearchParams(params);
    updated.delete("device");
    updated.delete("panel");
    setParams(updated);
  }, [params, setParams, markerKey]);
  const setPanel = useCallback(
    (next: DrawerPanel) => {
      const updated = new URLSearchParams(params);
      if (next === "status") updated.delete("panel");
      else updated.set("panel", next);
      setParams(updated, { replace: true });
    },
    [params, setParams],
  );
  return { target, panel, markerKey, open, close, setPanel };
}

function DrawerOverlay({ drawer }: { drawer: Drawer }) {
  if (drawer.target === null) return null;
  return (
    <DeviceDrawer
      target={drawer.target}
      panel={drawer.panel}
      onPanel={drawer.setPanel}
      onClose={drawer.close}
      onOpen={drawer.open}
    />
  );
}

export function Map() {
  const { deploymentId, podId } = useParams();
  // E6.7: the Overview links straight into a lens with `?tab=`.
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<MapTab>(() => tabFromUrl(searchParams.get("tab")));
  const drawer = useDrawer();
  const config = useQuery({ queryKey: mapKeys.config, queryFn: getMapConfig });

  // A Pod route names the Pod; the map reads its Deployment.
  const pod = useQuery({
    queryKey: ["pod", podId],
    queryFn: () => getPod(podId!),
    enabled: podId !== undefined,
  });
  const activeDeployment = deploymentId ?? pod.data?.deployment_id;

  const layer = useQuery({
    queryKey: mapKeys.deployments,
    queryFn: getMapDeployments,
    enabled: activeDeployment === undefined && podId === undefined,
    refetchInterval: MAP_POLL_MS,
  });
  const detail = useQuery({
    queryKey: mapKeys.deployment(activeDeployment ?? ""),
    queryFn: () => getMapDeployment(activeDeployment!),
    enabled: activeDeployment !== undefined,
    refetchInterval: MAP_POLL_MS,
  });

  const crumbs: Crumb[] = [{ label: "Organization", to: "/map" }];
  if (detail.data) {
    crumbs.push({
      label: detail.data.deployment.name,
      to: podId ? `/map/deployments/${detail.data.deployment.id}` : undefined,
    });
    const focused = detail.data.pods.find((row) => row.id === podId);
    if (focused) crumbs.push({ label: focused.name });
  } else if (activeDeployment === undefined && podId === undefined) {
    crumbs[0] = { label: "Organization" };
  }

  return (
    <>
      <ContextBar
        crumbs={crumbs}
        tabs={TABS}
        activeTab={tab}
        onTabChange={(next) => setTab(next as MapTab)}
      />
      <div className="map-region" data-testid="map-region">
        {config.isLoading ? (
          <div className="skeleton map-skeleton" data-testid="map-loading" />
        ) : config.data === undefined ? (
          <EmptyState title="The map could not load" testId="map-config-error">
            The basemap configuration did not arrive
            {config.error instanceof Error ? ` (${config.error.message})` : ""}. Check that the API
            is reachable and that you are signed in.
          </EmptyState>
        ) : activeDeployment === undefined && podId === undefined ? (
          <OrganizationLayer config={config.data} rows={layer.data} tab={tab} drawer={drawer} />
        ) : (
          <DeploymentLayer
            config={config.data}
            detail={detail.data}
            missing={detail.isError || pod.isError}
            podId={podId}
            tab={tab}
            drawer={drawer}
          />
        )}
        <div className="map-legend-slot">
          <StatusLegend />
        </div>
      </div>
    </>
  );
}

function TabNote({ tab }: { tab: MapTab }) {
  if (tab === "Alerts") {
    return (
      <p className="map-notice" data-testid="map-tab-note">
        Showing devices with open inventory alerts. Grafana alerts arrive with E7.
      </p>
    );
  }
  if (tab === "Reconciliation") {
    return (
      <p className="map-notice" data-testid="map-tab-note">
        Showing drifted devices and devices whose config failed to apply.
      </p>
    );
  }
  return null;
}

function OrganizationLayer({
  config,
  rows,
  tab,
  drawer,
}: {
  config: Parameters<typeof MapView>[0]["config"];
  rows: MapDeployment[] | undefined;
  tab: MapTab;
  drawer: Drawer;
}) {
  const navigate = useNavigate();
  const specs: MarkerSpec[] = useMemo(
    () =>
      (rows ?? []).filter(isPlaced).map((row) => ({
        key: `deployment:${row.id}`,
        lat: row.gps_lat,
        lon: row.gps_lon,
        size: DEPLOYMENT_SIZE,
        status: row.status,
        label: accessibleName("deployment", row.name, row.status, row.alert_count),
        html: deploymentMarkerHtml({
          name: row.name,
          status: row.status,
          alerts: row.alert_count,
          services: row.services_status,
          dimmed: isDimmed(tab, row.status, row.alert_count),
          selected: drawer.markerKey === `deployment:${row.id}`,
        }),
        onSelect: () => navigate(`/map/deployments/${row.id}`),
      })),
    [rows, tab, navigate, drawer.markerKey],
  );
  const unplaced: UnplacedItem[] = (rows ?? [])
    .filter((row) => !isPlaced(row))
    .map((row) => ({
      key: row.id,
      name: row.name,
      kind: "deployment",
      fixAt: `/inventory/deployments/${row.id}`,
      deploymentId: row.id,
    }));
  const points = specs.map((spec) => [spec.lat, spec.lon] as [number, number]);

  return (
    <MapView
      config={config}
      drawerOpen={drawer.target !== null}
      overlay={
        <>
          {rows !== undefined && specs.length === 0 && (
            <p className="map-notice" data-testid="map-no-devices">
              No deployments are placed on the map yet. Set a location on a deployment to see it
              here.
            </p>
          )}
          <TabNote tab={tab} />
          <UnplacedPanel items={unplaced} />
          <DrawerOverlay drawer={drawer} />
        </>
      }
    >
      <MarkerLayer specs={specs} cluster={false} />
      <LeaderLine markerKey={drawer.target ? drawer.markerKey : null} />
      <FitToPoints fitKey={`org:${points.length > 0}`} points={points} />
    </MapView>
  );
}

function DeploymentLayer({
  config,
  detail,
  missing,
  podId,
  tab,
  drawer,
}: {
  config: Parameters<typeof MapView>[0]["config"];
  detail: MapDeploymentDetail | undefined;
  missing: boolean;
  podId: string | undefined;
  tab: MapTab;
  drawer: Drawer;
}) {
  const navigate = useNavigate();
  const selected = drawer.markerKey;
  const [showLeaves, setShowLeaves] = useState(recallShowLeaves);
  const { open } = drawer;
  const selectListener = useCallback((mac: string) => open({ kind: "listener", id: mac }), [open]);

  const pods = detail?.pods ?? [];
  const listeners = detail?.listeners ?? [];
  const unplacedListenersByPod = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of listeners) {
      if (!isPlaced(row)) counts[row.pod_id] = (counts[row.pod_id] ?? 0) + 1;
    }
    return counts;
  }, [listeners]);

  const podSpecs: MarkerSpec[] = useMemo(
    () =>
      pods.filter(isPlaced).map((row) => ({
        key: `pod:${row.id}`,
        lat: row.gps_lat,
        lon: row.gps_lon,
        size: podMarkerSize(row.listener_count),
        status: row.status,
        label: accessibleName(
          "pod",
          row.name,
          row.status,
          row.alert_count,
          `${row.listener_count} listeners`,
        ),
        html: podMarkerHtml({
          name: row.name,
          status: row.status,
          alerts: row.alert_count,
          listeners: row.listener_count,
          unplacedListeners: unplacedListenersByPod[row.id] ?? 0,
          dimmed: isDimmed(tab, row.status, row.alert_count),
          showLabel: true,
          selected: selected === `pod:${row.id}`,
        }),
        // On its own route, a Pod opens its drawer; elsewhere it drills in.
        onSelect: () =>
          row.id === podId ? open({ kind: "pod", id: row.id }) : navigate(`/map/pods/${row.id}`),
      })),
    [pods, tab, unplacedListenersByPod, navigate, selected, podId, open],
  );

  const listenerSpecs: MarkerSpec[] = useMemo(
    () =>
      listeners.filter(isPlaced).map((row) => ({
        key: `listener:${row.mac}`,
        lat: row.gps_lat,
        lon: row.gps_lon,
        size: LISTENER_SIZE,
        status: row.status,
        label: accessibleName("listener", row.name, row.status, row.alert_count),
        html: listenerPinHtml({
          status: row.status,
          alerts: row.alert_count,
          dimmed: isDimmed(tab, row.status, row.alert_count),
          selected: selected === `listener:${row.mac}`,
        }),
        onSelect: () => selectListener(row.mac),
      })),
    [listeners, tab, selected, selectListener],
  );

  if (missing) {
    return (
      <EmptyState title="Not found" testId="map-deployment-missing">
        This deployment or pod does not exist, or it is outside your assigned scope.{" "}
        <Link to="/map">Back to the organization map</Link>
      </EmptyState>
    );
  }

  const focus = podId ? pods.find((row) => row.id === podId) : undefined;
  const points: [number, number][] = focus
    ? [
        ...(isPlaced(focus) ? [[focus.gps_lat, focus.gps_lon] as [number, number]] : []),
        ...listeners
          .filter((row) => row.pod_id === focus.id)
          .filter(isPlaced)
          .map((row) => [row.gps_lat, row.gps_lon] as [number, number]),
      ]
    : [...podSpecs, ...listenerSpecs].map((spec) => [spec.lat, spec.lon] as [number, number]);

  const unplaced: UnplacedItem[] = pods
    .filter((row) => !isPlaced(row))
    .map((row) => ({
      key: row.id,
      name: row.name,
      kind: "pod",
      fixAt: `/inventory/pods/${row.id}`,
      deploymentId: detail!.deployment.id,
      // A GPS-less Listener orbits its Pod; an unplaced Pod leaves it nowhere
      // to orbit, so it is counted here instead (E6.3).
      note:
        row.aggregator_id === null
          ? "no aggregator yet"
          : `${row.listener_count} listeners` +
            ((unplacedListenersByPod[row.id] ?? 0) > 0
              ? `, ${unplacedListenersByPod[row.id]} without GPS`
              : ""),
    }));

  const level: DrawerTarget | null = focus
    ? { kind: "pod", id: focus.id }
    : detail
      ? { kind: "deployment", id: detail.deployment.id }
      : null;

  return (
    <MapView
      config={config}
      drawerOpen={drawer.target !== null}
      overlay={
        <>
          {detail !== undefined && podSpecs.length === 0 && listenerSpecs.length === 0 && (
            <p className="map-notice" data-testid="map-no-devices">
              Nothing in this deployment is placed on the map yet.
            </p>
          )}
          <TabNote tab={tab} />
          <UnplacedPanel items={unplaced} />
          <label className="map-control map-leaf-toggle" data-testid="map-leaf-toggle">
            <input
              type="checkbox"
              checked={showLeaves}
              onChange={(event) => {
                setShowLeaves(event.target.checked);
                rememberShowLeaves(event.target.checked);
              }}
            />
            Show GPS-less
          </label>
          {level && (
            <button
              type="button"
              className="map-control map-details"
              data-testid="map-details"
              onClick={() => open(level)}
            >
              {level.kind === "pod" ? "Pod details" : "Deployment details"}
            </button>
          )}
          <DrawerOverlay drawer={drawer} />
        </>
      }
    >
      <MarkerLayer specs={listenerSpecs} cluster />
      {showLeaves && (
        <LeafFan
          pods={pods}
          listeners={listeners}
          focusPodId={podId}
          tab={tab}
          selected={selected}
          onSelect={selectListener}
        />
      )}
      <MarkerLayer specs={podSpecs} cluster={false} />
      <LeaderLine markerKey={drawer.target ? drawer.markerKey : null} />
      <FitToPoints
        fitKey={`${detail?.deployment.id ?? "loading"}:${podId ?? ""}:${points.length > 0}`}
        points={points}
      />
    </MapView>
  );
}
