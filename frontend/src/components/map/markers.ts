/**
 * What each map marker looks like and says (task E6.2b; spec 9.2; DES v2:
 * "Map markers encode level as shape, status as glyph, and alerts as a badge
 * with its own outline").
 *
 * Pure: data in, HTML string and accessible name out. Leaflet's `DivIcon`
 * takes raw HTML, and Deployment and Pod names are operator input, so **every
 * name passes through `escapeHtml`**. The tests hold that with a name made of
 * markup.
 *
 * Three channels, always (DES rule): `data-status` drives color and glyph
 * through the same CSS custom properties as `.status-chip`, and the label is
 * text. `unknown` gets NO `data-status` and draws a dash: a device that never
 * spoke has no color (D40, D60).
 */
import { RolledUpStatus, statusLabel, worstStatus } from "../StatusChip";
import type { ServicesStatus } from "../../lib/map";

export type MarkerLevel = "deployment" | "pod" | "listener";

/** The map's view tabs (S2). Status colors everything; the other two dim
 * whatever does not need attention under that lens. */
export type MapTab = "Status" | "Reconciliation" | "Alerts";

const LEVEL_NAMES: Record<MarkerLevel, string> = {
  deployment: "Deployment",
  pod: "Pod",
  listener: "Listener",
};

const SERVICES_LABELS: Record<ServicesStatus, string> = {
  unconfigured: "Services not set up",
  pending_verification: "Services not verified",
  verified: "Services verified",
  degraded: "Services degraded",
};

/** Each shape's box, in px, matching `app.css` (`--eoe-control-height-md`
 * for a Deployment and a cluster, `--eoe-space-5` for a pin). Leaflet makes
 * the focusable, clickable element exactly this size, centered on the point,
 * so the target IS the shape. */
export const DEPLOYMENT_SIZE = 38;
export const LISTENER_SIZE = 20;
export const CLUSTER_SIZE = 38;

export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** What a screen reader hears on focus: level, name, status, alerts. */
export function accessibleName(
  level: MarkerLevel,
  name: string,
  status: RolledUpStatus,
  alerts: number,
  extra?: string,
): string {
  const parts = [`${LEVEL_NAMES[level]} ${name}`, statusLabel(status)];
  if (alerts > 0) parts.push(alerts === 1 ? "1 alert" : `${alerts} alerts`);
  if (extra) parts.push(extra);
  return parts.join(", ");
}

/** Under the Reconciliation and Alerts lenses, what is fine fades back. The
 * map payload carries status, not revision state, so "needs reconciliation"
 * means `drifted` or `degraded` (a failed revision, D60). */
export function isDimmed(tab: MapTab, status: RolledUpStatus, alerts: number): boolean {
  if (tab === "Reconciliation") return status !== "drifted" && status !== "degraded";
  if (tab === "Alerts") return alerts === 0;
  return false;
}

function statusAttribute(status: RolledUpStatus): string {
  return status === "unknown" ? 'data-unknown="true"' : `data-status="${status}"`;
}

function glyph(status: RolledUpStatus): string {
  return status === "unknown"
    ? '<span class="map-marker-dash" aria-hidden="true">—</span>'
    : '<span class="status-glyph" aria-hidden="true"></span>';
}

function badge(alerts: number): string {
  return alerts > 0 ? `<span class="map-alert-badge" aria-hidden="true">${alerts}</span>` : "";
}

function classes(level: MarkerLevel, dimmed: boolean, selected: boolean): string {
  return [
    "map-marker",
    `map-marker-${level}`,
    dimmed ? "map-marker-dimmed" : "",
    selected ? "map-marker-selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

export function deploymentMarkerHtml(input: {
  name: string;
  status: RolledUpStatus;
  alerts: number;
  services: ServicesStatus;
  dimmed: boolean;
}): string {
  // D190: services status is its own chip, never the marker's color, and only
  // shown when it is not the healthy answer.
  const services =
    input.services === "verified"
      ? ""
      : `<span class="map-services-chip" data-services="${input.services}">${SERVICES_LABELS[input.services]}</span>`;
  return (
    `<div class="${classes("deployment", input.dimmed, false)}" ${statusAttribute(input.status)}>` +
    `<span class="map-marker-shape">${glyph(input.status)}${badge(input.alerts)}</span>` +
    `<span class="map-marker-label">${escapeHtml(input.name)}` +
    `<span class="map-marker-status">${statusLabel(input.status)}</span></span>` +
    services +
    `</div>`
  );
}

/** Pod squares grow with their Listener count, capped at spec 4.1's ~50. */
export function podMarkerSize(listeners: number): number {
  return 28 + Math.round((Math.min(Math.max(listeners, 0), 50) / 50) * 16);
}

export function podMarkerHtml(input: {
  name: string;
  status: RolledUpStatus;
  alerts: number;
  listeners: number;
  unplacedListeners: number;
  dimmed: boolean;
  showLabel: boolean;
}): string {
  const size = podMarkerSize(input.listeners);
  // In the label, not floating over the marker: the space above a Pod is
  // where its leaves fan out (E6.3).
  const noGps =
    input.unplacedListeners > 0
      ? ` · <span class="map-nogps-badge">${input.unplacedListeners} no GPS</span>`
      : "";
  const label = input.showLabel
    ? `<span class="map-marker-label">${escapeHtml(input.name)}` +
      `<span class="map-marker-status">${statusLabel(input.status)} · ${input.listeners} listeners${noGps}</span></span>`
    : "";
  return (
    `<div class="${classes("pod", input.dimmed, false)}" ${statusAttribute(input.status)}>` +
    `<span class="map-marker-shape" style="width:${size}px;height:${size}px">` +
    `${glyph(input.status)}${badge(input.alerts)}</span>` +
    label +
    `</div>`
  );
}

export function listenerPinHtml(input: {
  status: RolledUpStatus;
  alerts: number;
  dimmed: boolean;
  selected: boolean;
  /** A GPS-less Listener drawn beside its Pod (E6.3): hollow and dashed, so
   * it never reads as a surveyed position, even in greyscale. `dx`/`dy` are
   * its pixel offset FROM the Pod; the tether is drawn back along it. */
  leaf?: { dx: number; dy: number };
}): string {
  const leaf = input.leaf ? " map-marker-leaf" : "";
  // A CSS line from the leaf's center back to its Pod: no vector layer, so it
  // follows the theme, needs no re-projection, and renders anywhere.
  const tether = input.leaf
    ? `<span class="map-tether" aria-hidden="true" style="width:${Math.hypot(input.leaf.dx, input.leaf.dy).toFixed(1)}px;transform:rotate(${((Math.atan2(-input.leaf.dy, -input.leaf.dx) * 180) / Math.PI).toFixed(1)}deg)"></span>`
    : "";
  return (
    `<div class="${classes("listener", input.dimmed, input.selected)}${leaf}" ${statusAttribute(input.status)}>` +
    tether +
    `<span class="map-marker-shape">${glyph(input.status)}${badge(input.alerts)}</span>` +
    `</div>`
  );
}

/** A cluster of Listener pins: its count, and the worst status inside it,
 * computed with the severity order the server uses (pinned by a test). */
export function clusterHtml(statuses: RolledUpStatus[]): string {
  const worst = worstStatus(statuses);
  return (
    `<div class="map-cluster" ${statusAttribute(worst)}>` +
    `${glyph(worst)}<span class="map-cluster-count">${statuses.length}</span></div>`
  );
}
