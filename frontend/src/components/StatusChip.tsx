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

/**
 * A device's spec 9.3 status in a table cell (task E3.12; D60).
 *
 * **`unknown` is not a chip, deliberately.** A device that has been entered in
 * inventory but has never spoken has no status, and giving it a coloured dot —
 * any colour — would be the invented status D40 forbade. It renders as a
 * muted dash with the reason available to screen readers, which is honest and
 * visibly different from the six real states.
 */
export function StatusCell({ status }: { status: DeviceStatus | "unknown" }) {
  if (status === "unknown") {
    return (
      <span className="muted" title="This device has not reported yet">
        <span aria-hidden="true">—</span>
        <span className="visually-hidden">No status reported yet</span>
      </span>
    );
  }
  return <StatusChip status={status} />;
}
