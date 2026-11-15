/**
 * The Owner landing view (task E6.7; spec 10.3; V2·S1).
 *
 * E1.8 built this page with E1-owned numbers only and honest gaps naming the
 * epics that would fill them (project-changes #16). E6.7 fills them from the
 * map's reads (E6.4), so the Overview, the map and the inventory tables are
 * one derivation and cannot disagree (D60):
 *
 * - **Hero:** devices online / registered, from the Organization layer's
 *   counts. Online is every status except `offline`, `unreachable` (E6.9:
 *   nothing is heard from it) and `unknown`.
 * - **One card per Deployment:** its rollup, distribution, per-service chips
 *   (E5.5's status read, the one source of those), its Pods with status, and
 *   Open map.
 * - **Needs attention:** devices with open alerts, then offline, unreachable,
 *   degraded and drifted, worst first, capped, with links into the map.
 *
 * Telemetry figures (recordings collected, data age) are E7's, and the page
 * says so rather than inventing them. Live: the map queries it reads are the
 * ones E6.6 invalidates and polls, so this page updates without a reload too.
 */
import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Can } from "../components/Can";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { ServiceChip } from "../components/ServiceResultRow";
import {
  RolledUpStatus,
  STATUS_SEVERITY,
  StatusCell,
  StatusChip,
  statusLabel,
} from "../components/StatusChip";
import { listOrganizations } from "../lib/inventory";
import {
  MAP_POLL_MS,
  MapDeployment,
  MapDeploymentDetail,
  StatusCounts,
  getMapDeployment,
  getMapDeployments,
  mapKeys,
} from "../lib/map";
import {
  DESCRIPTOR_BY_KEY,
  SERVICE_KEYS,
  getServicesStatus,
  servicesStatusKey,
} from "../lib/services";

/** Not online: down, cut off behind a down Aggregator, or never heard. */
const NOT_ONLINE: RolledUpStatus[] = ["offline", "unreachable", "unknown"];

/** What the attention panel lists, worst first (the server's order). */
const NEEDS_ATTENTION: RolledUpStatus[] = [
  "alerting",
  "offline",
  "unreachable",
  "degraded",
  "drifted",
];

/** The panel shows this many; the rest are a link into the map away. */
export const ATTENTION_CAP = 8;

export function onlineTotals(rows: MapDeployment[]): { online: number; registered: number } {
  let registered = 0;
  let notOnline = 0;
  for (const row of rows) {
    for (const status of STATUS_SEVERITY) {
      registered += row.counts[status] ?? 0;
      if (NOT_ONLINE.includes(status)) notOnline += row.counts[status] ?? 0;
    }
  }
  return { online: registered - notOnline, registered };
}

export interface AttentionItem {
  key: string;
  status: RolledUpStatus;
  title: string;
  detail: string;
  to: string;
}

/**
 * The devices that need someone, from the Deployments' map reads. Listeners
 * cut off behind one offline Aggregator are one row per Pod, not one per
 * Listener: the Aggregator is the thing to fix, and fifty identical rows would
 * bury everything else.
 */
export function attentionItems(details: MapDeploymentDetail[]): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const detail of details) {
    const deployment = detail.deployment;
    const base = `/map/deployments/${deployment.id}`;
    const podName = new Map(detail.pods.map((pod) => [pod.id, pod.name]));
    for (const pod of detail.pods) {
      const status = pod.aggregator_status;
      if (pod.aggregator_id === null || status === null || !NEEDS_ATTENTION.includes(status)) {
        continue;
      }
      items.push({
        key: `aggregator:${pod.aggregator_id}`,
        status,
        title: pod.aggregator_name ?? pod.aggregator_uuid ?? "Aggregator",
        detail: `Aggregator · ${pod.name} · ${deployment.name}`,
        to: `${base}?device=aggregator:${pod.aggregator_id}`,
      });
    }
    const cutOff = new Map<string, number>();
    for (const listener of detail.listeners) {
      if (listener.status === "unreachable") {
        cutOff.set(listener.pod_id, (cutOff.get(listener.pod_id) ?? 0) + 1);
        continue;
      }
      if (!NEEDS_ATTENTION.includes(listener.status)) continue;
      items.push({
        key: `listener:${listener.mac}`,
        status: listener.status,
        title: listener.name,
        detail:
          `Listener · ${podName.get(listener.pod_id) ?? "Pod"} · ${deployment.name}` +
          (listener.alert_count > 0
            ? ` · ${listener.alert_count} open alert${listener.alert_count === 1 ? "" : "s"}`
            : ""),
        to: `${base}?device=listener:${listener.mac}`,
      });
    }
    for (const [podId, count] of cutOff) {
      items.push({
        key: `unreachable:${podId}`,
        status: "unreachable",
        title: `${count} listener${count === 1 ? "" : "s"} unreachable`,
        detail: `Behind an offline aggregator · ${podName.get(podId) ?? "Pod"} · ${deployment.name}`,
        to: `/map/pods/${podId}`,
      });
    }
    // Alerts naming the Deployment but no device in inventory (D191).
    const attached = detail.pods.reduce((sum, pod) => sum + pod.alert_count, 0);
    const unattached = deployment.alert_count - attached;
    if (unattached > 0) {
      items.push({
        key: `deployment-alerts:${deployment.id}`,
        status: "alerting",
        title: `${unattached} alert${unattached === 1 ? "" : "s"} not tied to a device`,
        detail: `${deployment.name} · usually an aggregator nobody has entered yet`,
        to: `${base}?device=deployment:${deployment.id}`,
      });
    }
  }
  const rank = (status: RolledUpStatus) => STATUS_SEVERITY.indexOf(status);
  return items.sort((a, b) => rank(a.status) - rank(b.status) || a.title.localeCompare(b.title));
}

export function Overview() {
  const organizations = useQuery({
    queryKey: ["organizations"],
    queryFn: () => listOrganizations(),
  });
  const layer = useQuery({
    queryKey: mapKeys.deployments,
    queryFn: getMapDeployments,
    refetchInterval: MAP_POLL_MS,
  });
  const rows = layer.data ?? [];
  const details = useQueries({
    queries: rows.map((row) => ({
      queryKey: mapKeys.deployment(row.id),
      queryFn: () => getMapDeployment(row.id),
      refetchInterval: MAP_POLL_MS,
    })),
  });

  const org = organizations.data?.items[0];

  if (layer.isLoading) {
    return (
      <div className="page" data-testid="overview-loading">
        <div className="skeleton skeleton-row" />
        <div className="skeleton skeleton-row" />
        <p className="skeleton-caption">Querying deployments · skeleton holds final geometry</p>
      </div>
    );
  }

  const { online, registered } = onlineTotals(rows);
  const pods = rows.reduce((sum, row) => sum + row.pod_count, 0);
  const listeners = rows.reduce((sum, row) => sum + row.listener_count, 0);
  const loaded = details.flatMap((query) => (query.data ? [query.data] : []));
  const detailById = new Map(loaded.map((detail) => [detail.deployment.id, detail]));

  return (
    <div className="page">
      <PageHeader eyebrow={org?.name ?? "Organization"} title="Organization overview">
        <div className="hero-metric" data-testid="overview-hero">
          <span className="eyebrow">Devices online</span>
          <span className="hero-number">
            {online}
            <span className="hero-denominator"> / {registered}</span>
          </span>
        </div>
        <Can permission="manage_devices">
          <Link className="btn-secondary" to="/inventory">
            Open inventory
          </Link>
        </Can>
      </PageHeader>
      <p className="scope-caption" data-testid="overview-meta">
        {rows.length} deployments · {pods} pods · {listeners} listeners
      </p>
      <p className="muted" data-testid="overview-telemetry">
        Recordings collected and data freshness arrive with E7, which wires each deployment&apos;s
        telemetry in.
      </p>
      {rows.length === 0 ? (
        <EmptyState title="No deployments yet" testId="overview-empty">
          A deployment groups pods around one telemetry stack. Create one under Inventory, or bring
          an existing site in from a spreadsheet.{" "}
          <Link to="/inventory/import">Import inventory CSV</Link>
        </EmptyState>
      ) : (
        <div className="overview-grid">
          <div className="overview-deployments" data-testid="overview-deployments">
            <span className="eyebrow">Deployments</span>
            {rows.map((row) => (
              <DeploymentCard key={row.id} row={row} detail={detailById.get(row.id)} />
            ))}
          </div>
          <AttentionPanel items={attentionItems(loaded)} />
        </div>
      )}
    </div>
  );
}

function Distribution({ counts }: { counts: StatusCounts }) {
  const present = STATUS_SEVERITY.filter((status) => counts[status] > 0);
  return (
    <p className="overview-chips" data-testid="overview-distribution">
      {present.map((status) =>
        status === "unknown" ? (
          <span key={status} className="muted">
            {counts[status]} not reported yet
          </span>
        ) : (
          <StatusChip key={status} status={status} count={counts[status]} />
        ),
      )}
    </p>
  );
}

function ServiceChips({ deploymentId }: { deploymentId: string }) {
  const status = useQuery({
    queryKey: servicesStatusKey(deploymentId),
    queryFn: () => getServicesStatus(deploymentId),
  });
  if (!status.data) return null;
  return (
    <p className="overview-chips" data-testid="overview-services">
      {SERVICE_KEYS.filter((key) => status.data.services[key] !== undefined).map((key) => (
        <span key={key} className="overview-service">
          {DESCRIPTOR_BY_KEY[key].label} <ServiceChip status={status.data.services[key].status} />
        </span>
      ))}
    </p>
  );
}

function DeploymentCard({
  row,
  detail,
}: {
  row: MapDeployment;
  detail: MapDeploymentDetail | undefined;
}) {
  return (
    <section className="card overview-card" data-testid={`overview-card-${row.slug}`}>
      <header className="overview-card-head">
        <h2>{row.name}</h2>
        <StatusCell status={row.status} />
      </header>
      <p className="muted">
        <span className="mono">{row.slug}</span> · {row.pod_count} pods · {row.listener_count}{" "}
        listeners
      </p>
      <Distribution counts={row.counts} />
      <ServiceChips deploymentId={row.id} />
      {detail && detail.pods.length > 0 && (
        <ul className="overview-pods" data-testid="overview-pods">
          {detail.pods.map((pod) => (
            <li key={pod.id}>
              <StatusCell status={pod.status} />
              <Link to={`/map/pods/${pod.id}`}>{pod.name}</Link>
              <span className="muted">{pod.listener_count} listeners</span>
            </li>
          ))}
        </ul>
      )}
      <div className="overview-card-actions">
        <Link className="btn-secondary" to={`/map/deployments/${row.id}`}>
          Open map
        </Link>
        <Link className="btn-tertiary" to={`/inventory/deployments/${row.id}`}>
          Open inventory
        </Link>
      </div>
    </section>
  );
}

function AttentionPanel({ items }: { items: AttentionItem[] }) {
  const shown = items.slice(0, ATTENTION_CAP);
  return (
    <div className="overview-attention" data-testid="overview-attention">
      <span className="eyebrow">Needs attention</span>
      {items.length === 0 ? (
        <EmptyState title="Nothing needs attention" testId="overview-attention-empty">
          No device has an open alert, and none is offline, unreachable, degraded or drifted.
          Grafana alerts join this list with E7.
        </EmptyState>
      ) : (
        <>
          <ul className="overview-attention-list">
            {shown.map((item) => (
              <li key={item.key} className="card overview-attention-item">
                <StatusChip status={item.status as Exclude<RolledUpStatus, "unknown">} />
                <Link to={item.to}>
                  <strong>{item.title}</strong>
                </Link>
                <span className="muted">{item.detail}</span>
              </li>
            ))}
          </ul>
          {items.length > shown.length && (
            <p className="muted" data-testid="overview-attention-more">
              {items.length - shown.length} more. Show all on the map:{" "}
              <Link to="/map?tab=alerts">Alerts</Link> ·{" "}
              <Link to="/map?tab=reconciliation">Reconciliation</Link>
            </p>
          )}
          <p className="muted">
            {statusLabel("alerting")} counts open inventory alerts today; Grafana alerts join with
            E7.
          </p>
        </>
      )}
    </div>
  );
}
