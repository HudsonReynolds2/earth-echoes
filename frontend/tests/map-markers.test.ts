/**
 * E6.2b: what a marker says (spec 9.2; DES v2; D40/D60, D190).
 *
 * Pure builders, so every rule is checked without a map: names are escaped
 * (DivIcon takes raw HTML and names are operator input), `unknown` carries
 * no color, the services chip follows D190, and the lenses dim what is fine.
 */
import { describe, expect, it } from "vitest";

import {
  accessibleName,
  clusterHtml,
  deploymentMarkerHtml,
  escapeHtml,
  isDimmed,
  listenerPinHtml,
  podMarkerHtml,
  podMarkerSize,
} from "../src/components/map/markers";

function parse(html: string): HTMLElement {
  const host = document.createElement("div");
  host.innerHTML = html;
  return host.firstElementChild as HTMLElement;
}

const base = { alerts: 0, dimmed: false };

describe("marker markup", () => {
  it("escapes operator-entered names, so a name cannot become markup", () => {
    const html = deploymentMarkerHtml({
      ...base,
      name: '<img src=x onerror="alert(1)">',
      status: "healthy",
      services: "verified",
    });
    const element = parse(html);
    expect(element.querySelector("img")).toBeNull();
    expect(element.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(escapeHtml(`a&b"c'd`)).toBe("a&amp;b&quot;c&#39;d");
  });

  it("gives a real status three channels: data-status, a glyph, and a word", () => {
    const element = parse(
      deploymentMarkerHtml({ ...base, name: "RC", status: "offline", services: "verified" }),
    );
    expect(element).toHaveAttribute("data-status", "offline");
    expect(element.querySelector(".status-glyph")).not.toBeNull();
    expect(element).toHaveTextContent("Offline");
  });

  it("never colors a device that has not spoken (D60)", () => {
    for (const html of [
      deploymentMarkerHtml({ ...base, name: "x", status: "unknown", services: "verified" }),
      podMarkerHtml({
        ...base,
        name: "p",
        status: "unknown",
        listeners: 3,
        unplacedListeners: 0,
        showLabel: true,
      }),
      listenerPinHtml({ ...base, status: "unknown", selected: false }),
      clusterHtml(["unknown", "unknown"]),
    ]) {
      const element = parse(html);
      expect(element.hasAttribute("data-status")).toBe(false);
      expect(element).toHaveAttribute("data-unknown", "true");
      expect(element.querySelector(".status-glyph")).toBeNull();
    }
  });

  it("shows a services chip only when services are not verified, never as a color (D190)", () => {
    const pending = parse(
      deploymentMarkerHtml({
        ...base,
        name: "x",
        status: "healthy",
        services: "pending_verification",
      }),
    );
    expect(pending.querySelector(".map-services-chip")).toHaveTextContent("Services not verified");
    expect(pending).toHaveAttribute("data-status", "healthy");
    const verified = parse(
      deploymentMarkerHtml({ ...base, name: "x", status: "healthy", services: "verified" }),
    );
    expect(verified.querySelector(".map-services-chip")).toBeNull();
  });

  it("badges alerts with their count, and only when there are any", () => {
    const pin = parse(
      listenerPinHtml({ alerts: 2, dimmed: false, status: "alerting", selected: false }),
    );
    expect(pin.querySelector(".map-alert-badge")).toHaveTextContent("2");
    const quiet = parse(listenerPinHtml({ ...base, status: "healthy", selected: false }));
    expect(quiet.querySelector(".map-alert-badge")).toBeNull();
  });

  it("sizes a Pod square by its Listener count, capped at ~50", () => {
    expect(podMarkerSize(0)).toBe(28);
    expect(podMarkerSize(25)).toBe(36);
    expect(podMarkerSize(50)).toBe(44);
    expect(podMarkerSize(500)).toBe(44);
    const element = parse(
      podMarkerHtml({
        ...base,
        name: "p",
        status: "healthy",
        listeners: 8,
        unplacedListeners: 4,
        showLabel: true,
      }),
    );
    expect(element.querySelector(".map-nogps-badge")).toHaveTextContent("4 no GPS");
    expect(element).toHaveTextContent("8 listeners");
  });

  it("draws a cluster as its count and the worst status inside it", () => {
    const cluster = parse(clusterHtml(["healthy", "sleeping", "drifted", "unknown"]));
    expect(cluster).toHaveAttribute("data-status", "drifted");
    expect(cluster).toHaveTextContent("4");
  });
});

describe("leaves", () => {
  it("draw hollow, with a tether back to the Pod along their own offset", () => {
    const leaf = parse(
      listenerPinHtml({ ...base, status: "healthy", selected: false, leaf: { dx: 0, dy: -48 } }),
    );
    expect(leaf).toHaveClass("map-marker-leaf");
    const tether = leaf.querySelector<HTMLElement>(".map-tether")!;
    // 48px long, pointing straight down from a leaf at 12 o'clock to its Pod.
    expect(tether.style.width).toBe("48px");
    expect(tether.style.transform).toBe("rotate(90.0deg)");
    const pin = parse(listenerPinHtml({ ...base, status: "healthy", selected: false }));
    expect(pin.querySelector(".map-tether")).toBeNull();
  });
});

describe("accessible names", () => {
  it("say level, name, status and alerts", () => {
    expect(accessibleName("pod", "Pod 01 · Alder Creek", "degraded", 1, "8 listeners")).toBe(
      "Pod Pod 01 · Alder Creek, Degraded, 1 alert, 8 listeners",
    );
    expect(accessibleName("listener", "alder-creek-02", "unknown", 3)).toBe(
      "Listener alder-creek-02, No status yet, 3 alerts",
    );
  });
});

describe("lenses", () => {
  it("Status dims nothing", () => {
    expect(isDimmed("Status", "healthy", 0)).toBe(false);
  });

  it("Reconciliation keeps drifted and failed-to-apply devices bright", () => {
    expect(isDimmed("Reconciliation", "drifted", 0)).toBe(false);
    expect(isDimmed("Reconciliation", "degraded", 0)).toBe(false);
    expect(isDimmed("Reconciliation", "healthy", 0)).toBe(true);
    expect(isDimmed("Reconciliation", "offline", 0)).toBe(true);
  });

  it("Alerts keeps only devices with an alert bright", () => {
    expect(isDimmed("Alerts", "alerting", 1)).toBe(false);
    expect(isDimmed("Alerts", "offline", 0)).toBe(true);
  });
});
