/**
 * The Owner landing view. Gate 27 built it from E1-owned numbers only (task
 * E1.8; project-changes #16); E6.7 (D204) fills it from the map's reads.
 *
 * The two gate-27 tests are AMENDED, not deleted, as phase-6 E6.7 requires:
 * the hero now reads devices online / registered instead of listeners
 * registered, and real status renders here, so "no [data-status]" became
 * "status comes from the map's rollup". The first-run empty state is
 * unchanged in what it offers.
 *
 * The map fixture (tests/map-fixture.ts): Redwood Coast has one offline
 * Aggregator (Ridge Line) and one alerting Listener; everything else has never
 * spoken. 6 Aggregators + 28 Listeners = 34 registered; only the alerting
 * Listener is heard from, so 1 is online.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { ATTENTION_CAP, attentionItems, onlineTotals } from "../src/pages/Overview";
import { FIXTURE_IDS } from "./inventory-fixture";
import { ALERTING_MAC, mapDeploymentFixture, mapDeploymentsFixture } from "./map-fixture";
import { mePayload, server } from "./msw-server";

function actAs(role: "owner" | "viewer" | "field_tech" = "owner") {
  const me =
    role === "owner" ? mePayload : { ...mePayload, assignments: [{ role, deployment_id: null }] };
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(me)));
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("organization overview", () => {
  it("hero reads devices online / registered, from the map's rollup (amended at E6.7)", async () => {
    actAs();
    renderAt("/");
    const hero = await screen.findByTestId("overview-hero");
    expect(hero).toHaveTextContent("Devices online");
    expect(hero).toHaveTextContent("1 / 34");
    expect(screen.getByTestId("overview-meta")).toHaveTextContent(
      "3 deployments · 6 pods · 28 listeners",
    );
    expect(screen.getByTestId("overview-telemetry")).toHaveTextContent("E7");
  });

  it("gives each Deployment its rollup, distribution, services, pods and Open map", async () => {
    actAs();
    renderAt("/");
    const card = await screen.findByTestId("overview-card-redwood-coast");
    expect(within(card).getByRole("heading", { name: "Redwood Coast" })).toBeInTheDocument();
    // The rollup is the map's own: an alerting Listener makes it alerting.
    expect(card.querySelector(".overview-card-head [data-status='alerting']")).not.toBeNull();
    const distribution = within(card).getByTestId("overview-distribution");
    expect(distribution).toHaveTextContent("1 alerting");
    expect(distribution).toHaveTextContent("1 offline");
    expect(distribution).toHaveTextContent("17 not reported yet");
    expect(await within(card).findByTestId("overview-services")).toHaveTextContent("Mosquitto");
    const pods = await within(card).findByTestId("overview-pods");
    expect(within(pods).getAllByRole("link")).toHaveLength(3);
    expect(within(pods).getByRole("link", { name: "Pod 02 · Ridge Line" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/map\/pods\//),
    );
    expect(within(card).getByRole("link", { name: "Open map" })).toHaveAttribute(
      "href",
      `/map/deployments/${FIXTURE_IDS.redwoodCoast}`,
    );
  });

  it("lists what needs attention, worst first, linked into the map's drawer", async () => {
    actAs();
    renderAt("/");
    const panel = await screen.findByTestId("overview-attention");
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(2));
    const [first, second] = within(panel).getAllByRole("listitem");
    expect(first).toHaveTextContent("alder-creek-03");
    expect(first).toHaveTextContent("1 open alert");
    expect(within(first).getByRole("link")).toHaveAttribute(
      "href",
      `/map/deployments/${FIXTURE_IDS.redwoodCoast}?device=listener:${ALERTING_MAC}`,
    );
    expect(second).toHaveTextContent("demo-agg-rc-02");
    expect(second.querySelector("[data-status='offline']")).not.toBeNull();
  });

  it.each(["viewer", "field_tech"] as const)(
    "shows a %s the page with no manage actions",
    async (role) => {
      actAs(role);
      renderAt("/");
      await screen.findByTestId("overview-card-redwood-coast");
      // The one manage action on this page is the header's inventory entry.
      const header = screen.getByTestId("overview-hero").parentElement!;
      expect(within(header).queryByRole("link", { name: "Open inventory" })).toBeNull();
      expect(screen.getByTestId("overview-hero")).toHaveTextContent("1 / 34");
    },
  );

  it("first-run empty state offers the S7 dual actions", async () => {
    actAs();
    server.use(http.get("http://api.test/api/v1/map/deployments", () => HttpResponse.json([])));
    renderAt("/");
    const emptyState = await screen.findByTestId("overview-empty");
    expect(emptyState).toHaveTextContent("A deployment groups pods around one telemetry stack");
    expect(within(emptyState).getByRole("link", { name: "Import inventory CSV" })).toHaveAttribute(
      "href",
      "/inventory/import",
    );
  });
});

describe("the attention list's rules", () => {
  const redwood = () => structuredClone(mapDeploymentFixture(FIXTURE_IDS.redwoodCoast)!);

  it("folds Listeners cut off behind one Aggregator into one row per Pod", () => {
    const detail = redwood();
    const ridge = detail.pods.find((pod) => pod.aggregator_uuid === "demo-agg-rc-02")!;
    for (const listener of detail.listeners) {
      if (listener.pod_id === ridge.id) listener.status = "unreachable";
    }
    const items = attentionItems([detail]);
    const cutOff = items.filter((item) => item.status === "unreachable");
    expect(cutOff).toHaveLength(1);
    expect(cutOff[0].title).toBe(`${ridge.listener_count} listeners unreachable`);
    expect(cutOff[0].to).toBe(`/map/pods/${ridge.id}`);
    // Worst first: the alerting Listener, the offline Aggregator, then these.
    expect(items.map((item) => item.status)).toEqual(["alerting", "offline", "unreachable"]);
  });

  it("names alerts that belong to no device", () => {
    const detail = redwood();
    detail.deployment.alert_count += 2;
    const orphan = attentionItems([detail]).find((item) => item.key.startsWith("deployment-"));
    expect(orphan?.title).toBe("2 alerts not tied to a device");
  });

  it("caps the panel and links the rest into the map's lenses", async () => {
    actAs();
    server.use(
      http.get("http://api.test/api/v1/map/deployments/:id", ({ params }) => {
        const detail = structuredClone(mapDeploymentFixture(String(params.id))!);
        for (const listener of detail.listeners) listener.status = "drifted";
        return HttpResponse.json(detail);
      }),
    );
    renderAt("/");
    const more = await screen.findByTestId("overview-attention-more");
    const panel = screen.getByTestId("overview-attention");
    expect(within(panel).getAllByRole("listitem")).toHaveLength(ATTENTION_CAP);
    expect(within(more).getByRole("link", { name: "Alerts" })).toHaveAttribute(
      "href",
      "/map?tab=alerts",
    );
    expect(within(more).getByRole("link", { name: "Reconciliation" })).toHaveAttribute(
      "href",
      "/map?tab=reconciliation",
    );
  });

  it("counts unreachable devices as not online", () => {
    const rows = mapDeploymentsFixture();
    const before = onlineTotals(rows);
    rows[0].counts.healthy += 3;
    expect(onlineTotals(rows).online).toBe(before.online + 3);
    rows[0].counts.unreachable += 4;
    expect(onlineTotals(rows)).toEqual({
      online: before.online + 3,
      registered: before.registered + 7,
    });
  });

  it("opens the map on the lens a link names", async () => {
    actAs();
    renderAt("/map?tab=reconciliation");
    expect(await screen.findByRole("tab", { name: "Reconciliation" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});
