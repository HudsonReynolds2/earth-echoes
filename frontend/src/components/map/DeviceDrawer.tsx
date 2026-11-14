/**
 * The map's device drawer (task E6.5; spec 9.2; V2·S2 "Map with the device
 * drawer open").
 *
 * V2 names four reasons the v1 drawer went unnoticed, and each one is fixed
 * here or by the map around it:
 *
 * 1. The map dims behind it and the selected marker keeps a halo (`Map.tsx`
 *    sets `data-drawer-open`, the marker builders set the selection).
 * 2. A leader line runs from the marker to this drawer (`LeaderLine.tsx`).
 * 3. A deep shadow and a 3px accent edge lift it off the map (CSS).
 * 4. A titled header with an explicit Close button and an Esc hint.
 *
 * **Every tab reads an endpoint that already exists**, gated the way that
 * endpoint is gated: Status is `GET /map/devices/...` (E6.5), Config is E2.4's
 * effective config through E2.7's `ProvenanceTable`, Timeline is E3.11's
 * `DeviceTimeline`, Card is E4.9's records through the filtered read, and
 * Telemetry says plainly that it arrives with E7. Nothing is invented.
 *
 * The drawer's state lives in the URL (`?device=` and `?panel=`), so a link
 * reopens it exactly, and a live refetch (E6.6) cannot close it.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { ReactNode, useEffect, useId, useRef } from "react";
import { Link } from "react-router-dom";

import { useCan } from "../Can";
import { EmptyState } from "../EmptyState";
import { ProvenanceTable } from "../ProvenanceTable";
import { RecordChip } from "../RecordChip";
import { ServicesSummary } from "../ServicesSummary";
import { DeviceTimeline } from "../DeviceTimeline";
import { STATUS_SEVERITY, StatusCell, StatusChip } from "../StatusChip";
import { ConfigEntity, getCatalog, getEffectiveConfig, getOverrides } from "../../lib/config";
import {
  DeviceKind,
  DrawerPanel,
  DrawerTarget,
  MapAggregator,
  MapAlert,
  MapDevice,
  getMapDevice,
  mapKeys,
  sendAggregatorCommand,
} from "../../lib/map";
import { deviceRecordsKey, listDeviceRecords } from "../../lib/provisioning";
import { getServicesStatus, servicesStatusKey } from "../../lib/services";
import type { TimelineTarget } from "../../lib/timeline";

const KIND_LABEL: Record<DeviceKind, string> = {
  listener: "Listener",
  aggregator: "Aggregator",
  pod: "Pod",
  deployment: "Deployment",
};

const PANEL_LABEL: Record<DrawerPanel, string> = {
  status: "Status",
  config: "Config",
  timeline: "Timeline",
  card: "Card",
  telemetry: "Telemetry",
};

const CONFIG_ENTITY: Record<DeviceKind, ConfigEntity> = {
  listener: "listeners",
  aggregator: "aggregators",
  pod: "pods",
  deployment: "deployments",
};

const SOURCE_LABEL: Record<string, string> = {
  organization: "the Organization",
  deployment: "the Deployment",
  pod: "the Pod",
  aggregator: "the Aggregator",
  listener: "this Listener",
  default: "the platform default",
};

/** A timestamp as the V2 drawer shows it: UTC, to the second, unambiguous. */
export function utc(iso: string | null): string {
  if (iso === null) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.toISOString().slice(0, 10)} ${date.toISOString().slice(11, 19)}Z`;
}

function humanize(text: string): string {
  const spaced = text.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** The device whose timeline and card stand for this target: a Pod's are its
 * Aggregator's, and a Deployment has neither. */
function deviceTarget(
  device: MapDevice,
): { timeline: TimelineTarget; card: ["aggregator" | "listener", string] } | null {
  if (device.entity_type === "listener") {
    return { timeline: { kind: "listener", mac: device.id }, card: ["listener", device.id] };
  }
  if (device.aggregator !== null && device.entity_type !== "deployment") {
    const id = device.aggregator.id;
    return { timeline: { kind: "aggregator", id }, card: ["aggregator", id] };
  }
  return null;
}

/** The tabs this target has. A tab whose source does not exist for it is
 * absent, never empty: a Deployment has no single timeline or card. */
export function panelsFor(
  kind: DeviceKind,
  hasDevice: boolean,
  canViewCards: boolean,
): DrawerPanel[] {
  const panels: DrawerPanel[] = ["status", "config"];
  if (kind !== "deployment" && hasDevice) {
    panels.push("timeline");
    if (canViewCards) panels.push("card");
  }
  panels.push("telemetry");
  return panels;
}

export function DeviceDrawer({
  target,
  panel,
  onPanel,
  onClose,
  onOpen,
}: {
  target: DrawerTarget;
  panel: DrawerPanel;
  onPanel: (panel: DrawerPanel) => void;
  onClose: () => void;
  onOpen: (target: DrawerTarget) => void;
}) {
  const titleId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const device = useQuery({
    queryKey: mapKeys.device(target.kind, target.id),
    queryFn: () => getMapDevice(target.kind, target.id),
  });
  const deploymentId = device.data?.deployment_id ?? null;
  const canViewCards = useCan("view_provisioning", deploymentId);

  // Focus moves in on open, and again when the drawer is pointed at another
  // device, so a keyboard user is never left on a marker behind it.
  useEffect(() => {
    titleRef.current?.focus();
  }, [target.kind, target.id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const data = device.data;
  const subject = data ? deviceTarget(data) : null;
  const panels = panelsFor(target.kind, subject !== null, canViewCards);
  const shown = panels.includes(panel) ? panel : "status";

  return (
    <aside
      className="device-drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      data-testid="device-drawer"
      data-device={`${target.kind}:${target.id}`}
    >
      <header className="device-drawer-header">
        <div className="device-drawer-heading">
          <span className="eyebrow">Device detail</span>
          <h2 id={titleId} ref={titleRef} tabIndex={-1} data-testid="device-drawer-title">
            {data?.name ?? KIND_LABEL[target.kind]}
          </h2>
          <p className="muted device-drawer-context">
            {KIND_LABEL[target.kind]}
            {data?.pod_name && target.kind !== "pod" ? ` · ${data.pod_name}` : ""}
            {data && target.kind === "pod" ? ` · ${data.deployment_name}` : ""}
          </p>
        </div>
        <span className="device-drawer-esc" aria-hidden="true">
          <kbd>Esc</kbd>
        </span>
        <button type="button" className="btn-secondary" onClick={onClose}>
          <span aria-hidden="true">✕ </span>Close
        </button>
      </header>

      {device.isError ? (
        <EmptyState title="Not found" testId="device-drawer-missing">
          This device does not exist, or it is outside your assigned scope.
        </EmptyState>
      ) : !data ? (
        <p className="muted device-drawer-body">Loading…</p>
      ) : (
        <>
          <Identity device={data} />
          <div className="tab-group device-drawer-tabs" role="tablist" aria-label="Device detail">
            {panels.map((name) => (
              <button
                key={name}
                type="button"
                role="tab"
                id={`${titleId}-${name}`}
                aria-selected={shown === name}
                aria-controls={`${titleId}-panel`}
                className="tab"
                onClick={() => onPanel(name)}
              >
                {PANEL_LABEL[name]}
              </button>
            ))}
          </div>
          <div
            className="device-drawer-body"
            role="tabpanel"
            id={`${titleId}-panel`}
            aria-labelledby={`${titleId}-${shown}`}
            data-testid={`device-drawer-${shown}`}
          >
            {shown === "status" && <StatusPanel device={data} onOpen={onOpen} />}
            {shown === "config" && <ConfigPanel device={data} />}
            {shown === "timeline" && subject && <DeviceTimeline target={subject.timeline} />}
            {shown === "card" && subject && (
              <CardPanel targetType={subject.card[0]} targetId={subject.card[1]} />
            )}
            {shown === "telemetry" && (
              <EmptyState title="Telemetry arrives with E7" testId="device-drawer-no-telemetry">
                Audio levels, buffer depth and link quality come from the deployment&apos;s
                Prometheus and InfluxDB. They are wired in with epic E7, so this tab shows no chart
                until there is real data to put in one.
              </EmptyState>
            )}
          </div>
          <Actions device={data} />
        </>
      )}
    </aside>
  );
}

function Identity({ device }: { device: MapDevice }) {
  const alerts = device.alerts.length;
  return (
    <div className="device-drawer-identity">
      {device.entity_type === "listener" && <span className="mono">{device.id}</span>}
      {device.entity_type === "aggregator" && device.aggregator && (
        <span className="mono">{device.aggregator.aggregator_uuid}</span>
      )}
      <span className="device-drawer-chips">
        <StatusCell status={device.status} />
        {alerts > 0 && <StatusChip status="alerting" count={alerts} />}
        {device.revision_state !== null && (
          <span className="revision-chip" data-revision-state={device.revision_state}>
            Config {device.revision_state}
          </span>
        )}
      </span>
    </div>
  );
}

function Facts({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="device-drawer-section">
      <h3 className="eyebrow">{title}</h3>
      <dl className="device-drawer-facts">{children}</dl>
    </section>
  );
}

function AlertList({ alerts }: { alerts: MapAlert[] }) {
  if (alerts.length === 0) return null;
  return (
    <section className="device-drawer-section" data-testid="device-drawer-alerts">
      <h3 className="eyebrow">Open alerts</h3>
      <ul className="device-drawer-alerts">
        {alerts.map((alert) => (
          <li key={alert.id} className="device-drawer-alert">
            <strong>
              <span aria-hidden="true">✕ </span>
              {humanize(alert.alert_type)}
            </strong>
            <span className="muted">
              {alert.entity_type} <span className="mono">{alert.entity_key}</span> · since{" "}
              {utc(alert.created_at)}
            </span>
            {alert.detail && Object.keys(alert.detail).length > 0 && (
              <dl className="device-drawer-facts">
                {Object.entries(alert.detail).map(([key, value]) => (
                  <Fact key={key} term={humanize(key)}>
                    <span className="mono">
                      {typeof value === "string" ? value : JSON.stringify(value)}
                    </span>
                  </Fact>
                ))}
              </dl>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </>
  );
}

function position(device: MapDevice): string {
  if (device.gps_lat === null || device.gps_lon === null) {
    return device.entity_type === "listener"
      ? "No GPS yet · drawn beside its pod"
      : "Not placed on the map";
  }
  return `${device.gps_lat}, ${device.gps_lon}`;
}

function onlineLine(aggregator: MapAggregator): string {
  if (aggregator.online === null) return "Never reported";
  const since = utc(aggregator.online_changed_at);
  return aggregator.online ? `Online since ${since}` : `Offline since ${since}`;
}

function AggregatorFacts({
  aggregator,
  onOpen,
  title,
}: {
  aggregator: MapAggregator;
  onOpen?: (target: DrawerTarget) => void;
  title: string;
}) {
  return (
    <Facts title={title}>
      <Fact term="Aggregator">
        {onOpen ? (
          <button
            type="button"
            className="btn-tertiary device-drawer-inline"
            onClick={() => onOpen({ kind: "aggregator", id: aggregator.id })}
          >
            {aggregator.name ?? aggregator.aggregator_uuid}
          </button>
        ) : (
          <span className="mono">{aggregator.aggregator_uuid}</span>
        )}
      </Fact>
      <Fact term="Status">
        <StatusCell status={aggregator.status} />
      </Fact>
      <Fact term="Link">{onlineLine(aggregator)}</Fact>
      <Fact term="Latest revision">{aggregator.revision_state ?? "None published yet"}</Fact>
    </Facts>
  );
}

function Counts({ device }: { device: MapDevice }) {
  if (device.counts === null) return null;
  const present = STATUS_SEVERITY.filter((status) => (device.counts?.[status] ?? 0) > 0);
  return (
    <section className="device-drawer-section" data-testid="device-drawer-counts">
      <h3 className="eyebrow">Devices</h3>
      <p className="device-drawer-chips">
        {present.map((status) =>
          status === "unknown" ? (
            <span key={status} className="muted">
              {device.counts![status]} not reported yet
            </span>
          ) : (
            <StatusChip key={status} status={status} count={device.counts![status]} />
          ),
        )}
      </p>
      <p className="muted">
        {device.pod_count !== null ? `${device.pod_count} pods · ` : ""}
        {device.listener_count ?? 0} listeners
      </p>
    </section>
  );
}

function StatusPanel({
  device,
  onOpen,
}: {
  device: MapDevice;
  onOpen: (target: DrawerTarget) => void;
}) {
  const services = useQuery({
    queryKey: servicesStatusKey(device.deployment_id),
    queryFn: () => getServicesStatus(device.deployment_id),
    enabled: device.entity_type === "deployment",
  });

  if (device.entity_type === "listener" && device.liveness) {
    const { liveness } = device;
    const grace = liveness.grace;
    return (
      <>
        <AlertList alerts={device.alerts} />
        <Facts title="Liveness">
          <Fact term="State">
            {liveness.state ?? "Not reported yet"}
            {liveness.changed_at ? ` · since ${utc(liveness.changed_at)}` : ""}
          </Fact>
          <Fact term="Last audio">{utc(liveness.last_audio_at)}</Fact>
          <Fact term="Expected wake">{utc(liveness.expected_wake_at)}</Fact>
          <Fact term="Grace">
            {grace.seconds === null ? "—" : `${grace.seconds}s`}{" "}
            <span className="muted">
              {grace.source === "listener"
                ? "set on this Listener"
                : `inherited from ${SOURCE_LABEL[grace.source] ?? grace.source}`}
            </span>
          </Fact>
          <Fact term="Last report">{utc(liveness.reported_at)}</Fact>
        </Facts>
        <Facts title="Placement">
          <Fact term="GPS">{position(device)}</Fact>
          <Fact term="Pod">{device.pod_name}</Fact>
          {device.aggregator && (
            <Fact term="Aggregator">
              <button
                type="button"
                className="btn-tertiary device-drawer-inline"
                onClick={() => onOpen({ kind: "aggregator", id: device.aggregator!.id })}
              >
                {device.aggregator.name ?? device.aggregator.aggregator_uuid}
              </button>
            </Fact>
          )}
        </Facts>
      </>
    );
  }
  if (device.entity_type === "aggregator" && device.aggregator) {
    return (
      <>
        <AlertList alerts={device.alerts} />
        <AggregatorFacts aggregator={device.aggregator} title="Link and revision" />
        <Facts title="Placement">
          <Fact term="GPS">{position(device)}</Fact>
          <Fact term="Pod">{device.pod_name}</Fact>
        </Facts>
      </>
    );
  }
  if (device.entity_type === "pod") {
    return (
      <>
        <AlertList alerts={device.alerts} />
        {device.aggregator ? (
          <AggregatorFacts aggregator={device.aggregator} onOpen={onOpen} title="Aggregator" />
        ) : (
          <p className="muted">This pod has no aggregator yet.</p>
        )}
        <Counts device={device} />
        <Facts title="Placement">
          <Fact term="GPS">{position(device)}</Fact>
        </Facts>
      </>
    );
  }
  return (
    <>
      <AlertList alerts={device.alerts} />
      <Counts device={device} />
      <section className="device-drawer-section">
        <h3 className="eyebrow">Services</h3>
        {services.isError ? (
          <p className="muted">The services status could not be loaded.</p>
        ) : (
          <ServicesSummary status={services.data} />
        )}
      </section>
      <Facts title="Placement">
        <Fact term="GPS">{position(device)}</Fact>
      </Facts>
    </>
  );
}

const NO_STAGED = new Map<string, unknown>();
const NO_ERRORS: Record<string, string> = {};
const ignore = () => undefined;

function ConfigPanel({ device }: { device: MapDevice }) {
  const entity = CONFIG_ENTITY[device.entity_type];
  const canEdit = useCan("manage_config", device.deployment_id);
  const catalog = useQuery({ queryKey: ["config", "catalog"], queryFn: getCatalog });
  const effective = useQuery({
    queryKey: ["config", "effective", entity, device.id],
    queryFn: () => getEffectiveConfig(entity, device.id),
  });
  const overrides = useQuery({
    queryKey: ["config", "overrides", entity, device.id],
    queryFn: () => getOverrides(entity, device.id),
  });

  if (effective.isError || catalog.isError) {
    return <p className="muted">The effective configuration could not be loaded.</p>;
  }
  if (!effective.data || !catalog.data) {
    return <p className="muted">Loading…</p>;
  }
  return (
    <>
      <p className="device-drawer-config-head">
        <span className="muted">
          Read-only. Each value shows the level it is resolved from; secrets are never shown.
        </span>
        {canEdit && (
          <Link
            to={`/configuration/${entity}/${encodeURIComponent(device.id)}`}
            data-testid="device-drawer-edit-config"
          >
            Edit at this level
          </Link>
        )}
      </p>
      <ProvenanceTable
        catalog={catalog.data.items}
        effective={effective.data}
        overrides={overrides.data?.overrides ?? {}}
        staged={NO_STAGED}
        level={device.entity_type}
        canEdit={false}
        onlyOverridden={false}
        errors={NO_ERRORS}
        onStage={ignore}
        onRevert={ignore}
      />
    </>
  );
}

function CardPanel({
  targetType,
  targetId,
}: {
  targetType: "aggregator" | "listener";
  targetId: string;
}) {
  const records = useQuery({
    queryKey: deviceRecordsKey(targetType, targetId),
    queryFn: () => listDeviceRecords(targetType, targetId),
  });
  if (records.isError) {
    return <p className="muted">Provisioning records could not be loaded.</p>;
  }
  if (!records.data) {
    return <p className="muted">Loading…</p>;
  }
  const latest = records.data.items[0];
  if (latest === undefined) {
    return (
      <EmptyState title="No card yet" testId="device-drawer-no-card">
        No provisioning bundle has included this {targetType} yet. Cards are generated on the{" "}
        <Link to="/provisioning">Provisioning</Link> page.
      </EmptyState>
    );
  }
  const generated = latest.status_history[0]?.at ?? null;
  return (
    <>
      <Facts title="Latest card">
        <Fact term="Status">
          <RecordChip status={latest.status} />
        </Fact>
        <Fact term="File">
          <span className="mono">{latest.file_path}</span>
        </Fact>
        <Fact term="Generated">{utc(generated)}</Fact>
        <Fact term="Config checksum">
          <span className="mono">{latest.config_checksum ?? "No revision at generation"}</span>
        </Fact>
      </Facts>
      {records.data.total > 1 && (
        <p className="muted">
          {records.data.total - 1} earlier card{records.data.total > 2 ? "s" : ""} for this{" "}
          {targetType}.
        </p>
      )}
    </>
  );
}

function inventoryPath(device: MapDevice): string {
  if (device.entity_type === "listener") {
    return `/inventory/listeners/${encodeURIComponent(device.id)}`;
  }
  if (device.entity_type === "deployment") return `/inventory/deployments/${device.id}`;
  return `/inventory/pods/${device.pod_id}`;
}

function Actions({ device }: { device: MapDevice }) {
  const aggregatorId =
    device.entity_type === "aggregator" || device.entity_type === "pod"
      ? (device.aggregator?.id ?? null)
      : null;
  const canCommand = useCan("manage_devices", device.deployment_id);
  const resync = useMutation({
    mutationFn: () => sendAggregatorCommand(aggregatorId!, "resync"),
  });
  return (
    <footer className="device-drawer-actions">
      <Link to={inventoryPath(device)} className="btn-tertiary">
        Open in inventory
      </Link>
      {aggregatorId !== null && canCommand && (
        <button
          type="button"
          className="btn-secondary"
          disabled={resync.isPending}
          onClick={() => resync.mutate()}
        >
          Resync
        </button>
      )}
      <span role="status" className="muted device-drawer-action-note">
        {resync.isSuccess && "Resync sent. The device's answer appears in its timeline."}
        {resync.isError && (resync.error as Error).message}
      </span>
    </footer>
  );
}
