/**
 * E6.5: the device drawer (spec 9.2; V2·S2). The phase document's acceptance:
 *
 * - each tab renders real fixture data;
 * - a Viewer sees no Resync and no Edit link; a Field Tech sees the Card tab
 *   but not the Config tab's Edit link;
 * - Esc closes the drawer, focus returns to the marker, and the halo follows
 *   the selection;
 * - a deep link to `?device=listener:…` opens the drawer on load;
 * - a secret config key shows the redacted marker and never a value.
 *
 * jsdom has no layout, so the leader line's geometry is tested as a pure
 * function here and drawn for real in `e2e/map.spec.ts`.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { leaderSegment } from "../src/components/map/LeaderLine";
import { parseDrawerTarget } from "../src/lib/map";
import { FIXTURE_IDS, aggregators } from "./inventory-fixture";
import { ALERTING_MAC, mapDeviceFixture } from "./map-fixture";
import { mePayload, server } from "./msw-server";

type Role = "owner" | "viewer" | "field_tech";

function actAs(role: Role) {
  const me =
    role === "owner" ? mePayload : { ...mePayload, assignments: [{ role, deployment_id: null }] };
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(me)));
}

let lastLocation = "";
function LocationProbe() {
  const location = useLocation();
  // URLSearchParams writes `:` as `%3A`; compare what a person would read.
  lastLocation = decodeURIComponent(location.pathname + location.search);
  return null;
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
      <LocationProbe />
    </MemoryRouter>,
  );
}

const ALDER = `/map/pods/${FIXTURE_IDS.alderCreekPod}`;
/** alder-creek-02: odd index, so no GPS, so a leaf on its Pod's route (E6.3).
 * Pins cluster in jsdom (no size, wide zoom); leaves never do. */
const LEAF_MAC = "02:EE:0E:01:01:02";
const RIDGE_AGG = aggregators.find((row) => row.aggregator_uuid === "demo-agg-rc-02")!;

async function drawer(): Promise<HTMLElement> {
  return screen.findByTestId("device-drawer");
}

function markerFor(key: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-marker-key="${key}"]`);
}

afterEach(() => window.localStorage.clear());

describe("opening the drawer", () => {
  it("opens from a deep link, focused, on the Status tab with liveness and alerts", async () => {
    actAs("owner");
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}`);
    const panel = await drawer();
    const title = within(panel).getByTestId("device-drawer-title");
    await waitFor(() => expect(title).toHaveTextContent("alder-creek-03"));
    expect(title).toHaveFocus();
    expect(panel).toHaveTextContent("Listener · Pod 01 · Alder Creek");
    expect(panel).toHaveTextContent(ALERTING_MAC);
    expect(within(panel).getByRole("button", { name: /Close/ })).toBeInTheDocument();
    expect(panel.querySelector("kbd")).toHaveTextContent("Esc");

    const status = within(panel).getByTestId("device-drawer-status");
    expect(status).toHaveTextContent("sleeping");
    expect(status).toHaveTextContent("Last audio2026-11-14 11:58:04Z");
    expect(status).toHaveTextContent("Expected wake2026-11-14 12:05:00Z");
    expect(status).toHaveTextContent("30s inherited from the Deployment");
    const alerts = within(status).getByTestId("device-drawer-alerts");
    expect(alerts).toHaveTextContent("Duplicate identity");
    expect(alerts).toHaveTextContent("demo-agg-rc-02");
  });

  it("opens on a leaf's click, with the halo on that marker and the map dimmed", async () => {
    actAs("owner");
    renderAt(ALDER);
    // On a Pod's own route its GPS-less Listeners are leaves (E6.3).
    await waitFor(() => expect(markerFor(`listener:${LEAF_MAC}`)).not.toBeNull());
    await userEvent.click(markerFor(`listener:${LEAF_MAC}`)!);
    await drawer();
    expect(lastLocation).toContain(`device=listener:${LEAF_MAC}`);
    expect(screen.getByTestId("map-view")).toHaveAttribute("data-drawer-open", "true");
    await waitFor(() =>
      expect(
        markerFor(`listener:${LEAF_MAC}`)!.querySelector(".map-marker-selected"),
      ).not.toBeNull(),
    );
  });

  it("opens the Pod from its Details control, and moves the halo to the Pod", async () => {
    actAs("owner");
    renderAt(ALDER);
    await userEvent.click(await screen.findByTestId("map-details"));
    const panel = await drawer();
    await waitFor(() =>
      expect(within(panel).getByTestId("device-drawer-title")).toHaveTextContent(
        "Pod 01 · Alder Creek",
      ),
    );
    await waitFor(() =>
      expect(
        markerFor(`pod:${FIXTURE_IDS.alderCreekPod}`)!.querySelector(".map-marker-selected"),
      ).not.toBeNull(),
    );
    expect(within(panel).getByTestId("device-drawer-counts")).toHaveTextContent("listeners");
  });
});

describe("a listener behind an offline aggregator (E6.9)", () => {
  it("reads Unreachable, says why, and labels its liveness as history", async () => {
    actAs("owner");
    server.use(
      http.get(`http://api.test/api/v1/map/devices/listener/:mac`, ({ params }) => {
        const device = mapDeviceFixture("listener", decodeURIComponent(String(params.mac)))!;
        return HttpResponse.json({
          ...device,
          status: "unreachable",
          aggregator: {
            ...device.aggregator!,
            status: "offline",
            online: false,
            online_changed_at: "2026-11-14T12:04:31Z",
          },
        });
      }),
    );
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}`);
    const panel = await drawer();
    const note = await within(panel).findByTestId("device-drawer-unreachable");
    expect(note).toHaveTextContent("demo-agg-rc-01");
    expect(note).toHaveTextContent("offline since 2026-11-14 12:04:31Z");
    expect(panel.querySelector(".status-chip[data-status='unreachable']")).toHaveTextContent(
      "Unreachable",
    );
    expect(within(panel).getByTestId("device-drawer-status")).toHaveTextContent(
      "Last reported liveness",
    );
  });
});

describe("closing the drawer", () => {
  it("closes on Esc, clears the URL, and returns focus to the marker", async () => {
    actAs("owner");
    renderAt(`${ALDER}?device=listener:${LEAF_MAC}&panel=config`);
    await drawer();
    await waitFor(() => expect(markerFor(`listener:${LEAF_MAC}`)).not.toBeNull());
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("device-drawer")).toBeNull());
    expect(lastLocation).not.toContain("device=");
    expect(lastLocation).not.toContain("panel=");
    expect(markerFor(`listener:${LEAF_MAC}`)).toHaveFocus();
    expect(screen.getByTestId("map-view")).not.toHaveAttribute("data-drawer-open");
  });
});

describe("the tabs", () => {
  it("renders every tab from real fixture data, and keeps the tab in the URL", async () => {
    actAs("owner");
    server.use(
      http.get("http://api.test/api/v1/:entity/:id/timeline", () =>
        HttpResponse.json({
          items: [
            {
              id: "e1",
              revision_id: "r1",
              from_state: "pending",
              to_state: "applied",
              trigger: "device_report",
              actor_user_id: null,
              actor_email: null,
              at: "2026-11-14T10:00:00Z",
              diff: null,
              detail: null,
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        }),
      ),
      http.get("http://api.test/api/v1/provisioning/records", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("target_type")).toBe("listener");
        expect(url.searchParams.get("target_id")).toBe(ALERTING_MAC);
        return HttpResponse.json({
          items: [
            {
              id: "rec-2",
              target_type: "listener",
              target_id: ALERTING_MAC,
              pod_id: FIXTURE_IDS.alderCreekPod,
              aggregator_uuid: "demo-agg-rc-01",
              file_kind: "listener_file",
              file_path: "pod-01/listeners/alder-creek-03.bin",
              config_revision_id: null,
              config_checksum: "sha256:abc",
              status: "confirmed",
              status_history: [{ status: "generated", at: "2026-11-10T08:00:00Z", actor: null }],
            },
          ],
          total: 2,
          limit: 5,
          offset: 0,
        });
      }),
    );
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}`);
    const panel = await drawer();
    const tab = (name: string) => within(panel).getByRole("tab", { name });
    await waitFor(() => expect(tab("Config")).toBeInTheDocument());
    expect(
      within(panel)
        .getAllByRole("tab")
        .map((t) => t.textContent),
    ).toEqual(["Status", "Config", "Timeline", "Card", "Telemetry"]);

    await userEvent.click(tab("Config"));
    expect(lastLocation).toContain("panel=config");
    const config = await within(panel).findByTestId("provenance-table");
    expect(config).toHaveTextContent("audio.sample_rate_hz");

    await userEvent.click(tab("Timeline"));
    const timeline = await within(panel).findByTestId("device-timeline");
    await waitFor(() => expect(timeline).toHaveTextContent("applied"));

    await userEvent.click(tab("Card"));
    const card = await within(panel).findByTestId("device-drawer-card");
    await waitFor(() => expect(card).toHaveTextContent("Confirmed"));
    expect(card).toHaveTextContent("pod-01/listeners/alder-creek-03.bin");
    expect(card).toHaveTextContent("2026-11-10 08:00:00Z");
    expect(card).toHaveTextContent("1 earlier card for this listener");

    await userEvent.click(tab("Telemetry"));
    expect(within(panel).getByTestId("device-drawer-no-telemetry")).toHaveTextContent("E7");
    expect(within(panel).queryByRole("img")).toBeNull();
  });

  it("says plainly when a device has no card yet", async () => {
    actAs("owner");
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}&panel=card`);
    expect(await screen.findByTestId("device-drawer-no-card")).toHaveTextContent(
      "No provisioning bundle has included this listener yet",
    );
  });

  it("never shows a secret's value, only the redacted marker", async () => {
    actAs("owner");
    // Alder Creek's Pod carries the E2 fixture's secret override.
    renderAt(`${ALDER}?device=pod:${FIXTURE_IDS.alderCreekPod}&panel=config`);
    const table = await screen.findByTestId("provenance-table");
    const row = within(table).getByText("network.wifi_password").closest("tr")!;
    expect(row).toHaveTextContent("••••••••");
    expect(table.textContent).not.toContain("$secret_set");
  });

  it("shows a Deployment's counts and service chips, and no timeline or card", async () => {
    actAs("owner");
    renderAt(
      `/map/deployments/${FIXTURE_IDS.redwoodCoast}?device=deployment:${FIXTURE_IDS.redwoodCoast}`,
    );
    const panel = await drawer();
    await waitFor(() =>
      expect(within(panel).getByTestId("device-drawer-title")).toHaveTextContent("Redwood Coast"),
    );
    expect(
      within(panel)
        .getAllByRole("tab")
        .map((t) => t.textContent),
    ).toEqual(["Status", "Config", "Telemetry"]);
    expect(await within(panel).findByTestId("services-summary")).toBeInTheDocument();
    expect(within(panel).getByTestId("device-drawer-counts")).toHaveTextContent("3 pods");
  });
});

describe("roles and actions", () => {
  it("lets an owner resync an Aggregator, reached from its Pod", async () => {
    actAs("owner");
    let sent: unknown = null;
    server.use(
      http.post(
        `http://api.test/api/v1/aggregators/${RIDGE_AGG.id}/commands`,
        async ({ request }) => {
          sent = await request.json();
          return HttpResponse.json(
            { command_id: "c1", published_at: "2026-11-14T12:00:00Z" },
            { status: 202 },
          );
        },
      ),
    );
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}?device=aggregator:${RIDGE_AGG.id}`);
    const panel = await drawer();
    await waitFor(() => expect(panel).toHaveTextContent("Offline since 2026-11-14 03:00:00Z"));
    await userEvent.click(within(panel).getByRole("button", { name: "Resync" }));
    await waitFor(() => expect(panel).toHaveTextContent("Resync sent"));
    expect(sent).toEqual({ command: "resync" });
  });

  it("walks from a Listener to its Aggregator", async () => {
    actAs("owner");
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}`);
    const panel = await drawer();
    await userEvent.click(await within(panel).findByRole("button", { name: "demo-agg-rc-01" }));
    await waitFor(() => expect(lastLocation).toContain("device=aggregator:"));
    await waitFor(() =>
      expect(within(panel).getByTestId("device-drawer-title")).toHaveTextContent("demo-agg-rc-01"),
    );
  });

  it("shows a Viewer no Resync and no Edit link", async () => {
    actAs("viewer");
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}?device=aggregator:${RIDGE_AGG.id}`);
    const panel = await drawer();
    await waitFor(() => expect(panel).toHaveTextContent("Offline since"));
    expect(within(panel).queryByRole("button", { name: "Resync" })).toBeNull();
    await userEvent.click(within(panel).getByRole("tab", { name: "Config" }));
    await within(panel).findByTestId("provenance-table");
    expect(within(panel).queryByTestId("device-drawer-edit-config")).toBeNull();
  });

  it("shows a Field Tech the Card tab but not the Edit link", async () => {
    actAs("field_tech");
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}&panel=config`);
    const panel = await drawer();
    await within(panel).findByTestId("provenance-table");
    expect(within(panel).queryByTestId("device-drawer-edit-config")).toBeNull();
    expect(within(panel).getByRole("tab", { name: "Card" })).toBeInTheDocument();
  });

  it("offers an Operator the Edit link at the device's own level", async () => {
    actAs("owner");
    renderAt(`${ALDER}?device=listener:${ALERTING_MAC}&panel=config`);
    const link = await screen.findByTestId("device-drawer-edit-config");
    expect(link).toHaveAttribute(
      "href",
      `/configuration/listeners/${encodeURIComponent(ALERTING_MAC)}`,
    );
  });
});

describe("the URL grammar and the leader line's geometry", () => {
  it("parses a MAC's colons as part of the id", () => {
    expect(parseDrawerTarget(`listener:${ALERTING_MAC}`)).toEqual({
      kind: "listener",
      id: ALERTING_MAC,
    });
    expect(parseDrawerTarget("organization:x")).toBeNull();
    expect(parseDrawerTarget("pod:")).toBeNull();
    expect(parseDrawerTarget(null)).toBeNull();
  });

  const rect = (left: number, top: number, width: number, height: number) =>
    ({ left, top, width, height, right: left + width, bottom: top + height }) as DOMRect;
  const view = rect(0, 0, 1000, 600);
  const drawerRect = rect(560, 16, 420, 568);

  it("joins the marker's center to the drawer's edge", () => {
    expect(leaderSegment(view, view, rect(290, 290, 20, 20), drawerRect)).toEqual({
      x1: 300,
      y1: 300,
      x2: 560,
      y2: 300,
    });
  });

  it("keeps the end on the drawer, clear of its corners", () => {
    expect(leaderSegment(view, view, rect(90, 2, 20, 20), drawerRect)?.y2).toBe(48);
  });

  it("hides the line for a marker out of view, under the drawer, or unmeasured", () => {
    expect(leaderSegment(view, view, rect(-50, 300, 20, 20), drawerRect)).toBeNull();
    expect(leaderSegment(view, view, rect(700, 300, 20, 20), drawerRect)).toBeNull();
    expect(leaderSegment(view, view, rect(0, 0, 0, 0), drawerRect)).toBeNull();
  });
});
