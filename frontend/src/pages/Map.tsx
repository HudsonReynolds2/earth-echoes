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
 */
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
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
import { FitToPoints, MarkerLayer, MarkerSpec } from "../components/map/MarkerLayer";
import { MapView } from "../components/map/MapView";
import { UnplacedItem, UnplacedPanel } from "../components/map/UnplacedPanel";
import { StatusCell, StatusLegend } from "../components/StatusChip";
import { getPod } from "../lib/inventory";
import {
  MapDeployment,
  MapDeploymentDetail,
  getMapConfig,
  getMapDeployment,
  getMapDeployments,
  isPlaced,
  mapKeys,
} from "../lib/map";

const TABS: MapTab[] = ["Status", "Reconciliation", "Alerts"];

export function Map() {
  const { deploymentId, podId } = useParams();
  const [tab, setTab] = useState<MapTab>("Status");
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
  });
  const detail = useQuery({
    queryKey: mapKeys.deployment(activeDeployment ?? ""),
    queryFn: () => getMapDeployment(activeDeployment!),
    enabled: activeDeployment !== undefined,
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
          <OrganizationLayer config={config.data} rows={layer.data} tab={tab} />
        ) : (
          <DeploymentLayer
            config={config.data}
            detail={detail.data}
            missing={detail.isError || pod.isError}
            podId={podId}
            tab={tab}
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
}: {
  config: Parameters<typeof MapView>[0]["config"];
  rows: MapDeployment[] | undefined;
  tab: MapTab;
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
        }),
        onSelect: () => navigate(`/map/deployments/${row.id}`),
      })),
    [rows, tab, navigate],
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
        </>
      }
    >
      <MarkerLayer specs={specs} cluster={false} />
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
}: {
  config: Parameters<typeof MapView>[0]["config"];
  detail: MapDeploymentDetail | undefined;
  missing: boolean;
  podId: string | undefined;
  tab: MapTab;
}) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const selected = params.get("device");

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
        }),
        onSelect: () => navigate(`/map/pods/${row.id}`),
      })),
    [pods, tab, unplacedListenersByPod, navigate],
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
        onSelect: () => {
          const next = new URLSearchParams(params);
          next.set("device", `listener:${row.mac}`);
          setParams(next);
        },
      })),
    [listeners, tab, selected, params, setParams],
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
      note: row.aggregator_id === null ? "no aggregator yet" : `${row.listener_count} listeners`,
    }));

  const chosen = selected?.startsWith("listener:")
    ? listeners.find((row) => `listener:${row.mac}` === selected)
    : undefined;

  return (
    <MapView
      config={config}
      overlay={
        <>
          {detail !== undefined && podSpecs.length === 0 && listenerSpecs.length === 0 && (
            <p className="map-notice" data-testid="map-no-devices">
              Nothing in this deployment is placed on the map yet.
            </p>
          )}
          <TabNote tab={tab} />
          <UnplacedPanel items={unplaced} />
          {chosen && (
            <section className="map-control map-selected" data-testid="map-selected">
              <span className="eyebrow">Listener</span>
              <strong>{chosen.name}</strong>
              <span className="mono">{chosen.mac}</span>
              <StatusCell status={chosen.status} />
              <Link to={`/inventory/listeners/${encodeURIComponent(chosen.mac)}`}>
                Open in inventory
              </Link>
              <button
                type="button"
                className="btn-tertiary"
                onClick={() => {
                  const next = new URLSearchParams(params);
                  next.delete("device");
                  setParams(next);
                }}
              >
                Close
              </button>
            </section>
          )}
        </>
      }
    >
      <MarkerLayer specs={listenerSpecs} cluster />
      <MarkerLayer specs={podSpecs} cluster={false} />
      <FitToPoints
        fitKey={`${detail?.deployment.id ?? "loading"}:${podId ?? ""}:${points.length > 0}`}
        points={points}
      />
    </MapView>
  );
}
