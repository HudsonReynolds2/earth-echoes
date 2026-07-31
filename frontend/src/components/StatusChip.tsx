/**
 * The six-state device status vocabulary (spec §9.3/§6.2, tokens D21), closed.
 *
 * Status is THREE channels, never one: color + shape glyph + text label. The
 * glyph is supplied by CSS from --eoe-color-status-{name}-glyph so the palette
 * survives greyscale and every form of color blindness. Rendering only the
 * color would make this component inaccessible.
 */
export const DEVICE_STATUSES = [
  "healthy",
  "sleeping",
  "degraded",
  "offline",
  "alerting",
  "drifted",
] as const;

export type DeviceStatus = (typeof DEVICE_STATUSES)[number];

const LABELS: Record<DeviceStatus, string> = {
  healthy: "Streaming",
  sleeping: "Sleeping",
  degraded: "Degraded",
  offline: "Offline",
  alerting: "Alerting",
  drifted: "Drifted",
};

export function StatusChip({ status, count }: { status: DeviceStatus; count?: number }) {
  return (
    <span className="status-chip" data-status={status}>
      <span className="status-glyph" aria-hidden="true" />
      {count === undefined ? LABELS[status] : `${count} ${LABELS[status].toLowerCase()}`}
    </span>
  );
}

/** Map legend: every status, always all six, so the vocabulary is learnable
 * from any screen that shows markers. */
export function StatusLegend() {
  return (
    <div className="status-legend" data-testid="status-legend">
      <span className="eyebrow">Legend</span>
      <div className="status-legend-items">
        {DEVICE_STATUSES.map((status) => (
          <StatusChip key={status} status={status} />
        ))}
      </div>
    </div>
  );
}
