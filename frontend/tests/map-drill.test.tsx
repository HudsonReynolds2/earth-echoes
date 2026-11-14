/**
 * E6.2b: drilling the map (spec 9.2). Organization → Deployment → Pod, each a
 * route; markers carry accessible names; unplaced levels are listed with a
 * way to fix them; the lenses dim what is fine; a Listener is selectable.
 *
 * jsdom has no layout, so Leaflet positions are meaningless here. These
 * assert what is in the marker pane and what clicking does. Real rendering is
 * `e2e/map.spec.ts`.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS } from "./inventory-fixture";
import { ALERTING_MAC } from "./map-fixture";
import { mePayload, server } from "./msw-server";

function actAs(role: "owner" | "viewer") {
  const me =
    role === "owner"
      ? mePayload
      : { ...mePayload, assignments: [{ role: "viewer", deployment_id: null }] };
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

function marker(name: RegExp): HTMLElement {
  const found = [
    ...document.querySelectorAll<HTMLElement>(".leaflet-marker-pane [aria-label]"),
  ].find((element) => name.test(element.getAttribute("aria-label") ?? ""));
  if (!found) throw new Error(`no marker named ${name}`);
  return found;
}

async function findMarker(name: RegExp): Promise<HTMLElement> {
  let found: HTMLElement | null = null;
  await waitFor(() => {
    found = marker(name);
  });
  return found!;
}

describe("the organization layer", () => {
  afterEach(() => window.localStorage.clear());

  it("draws one marker per placed Deployment, named for a screen reader", async () => {
    actAs("owner");
    renderAt("/map");
    const redwood = await findMarker(/^Deployment Redwood Coast/);
    expect(redwood.getAttribute("aria-label")).toBe("Deployment Redwood Coast, Alerting, 1 alert");
    expect(redwood).toHaveAttribute("role", "button");
    expect(redwood.querySelector("[data-status='alerting']")).not.toBeNull();
    const desert = marker(/^Deployment High Desert/);
    // Nothing has spoken: no colour, and the unverified services are a chip.
    expect(desert.querySelector("[data-unknown]")).not.toBeNull();
    expect(desert.querySelector(".map-services-chip")).toHaveTextContent("Services not verified");
  });

  it("lists an unplaced Deployment with a link to place it", async () => {
    actAs("owner");
    renderAt("/map");
    const panel = await screen.findByTestId("map-unplaced");
    expect(panel).toHaveTextContent("Unplaced Ridge");
    expect(within(panel).getByRole("link", { name: "Set location" })).toHaveAttribute(
      "href",
      "/inventory/deployments/d3000000-0000-4000-8000-000000000003",
    );
    expect(() => marker(/Unplaced Ridge/)).toThrow();
  });

  it("does not offer a viewer the fix, but still says what is missing", async () => {
    actAs("viewer");
    renderAt("/map");
    const panel = await screen.findByTestId("map-unplaced");
    expect(within(panel).queryByRole("link")).toBeNull();
    expect(panel).toHaveTextContent("No coordinates");
  });

  it("drills into a Deployment from the keyboard", async () => {
    actAs("owner");
    renderAt("/map");
    const redwood = await findMarker(/^Deployment Redwood Coast/);
    redwood.focus();
    await userEvent.keyboard("{Enter}");
    const crumbs = await screen.findByRole("navigation", { name: "Hierarchy" });
    await waitFor(() => expect(crumbs).toHaveTextContent("Redwood Coast"));
  });

  it("drills into a Deployment on click", async () => {
    actAs("owner");
    renderAt("/map");
    await userEvent.click(await findMarker(/^Deployment Redwood Coast/));
    const crumbs = await screen.findByRole("navigation", { name: "Hierarchy" });
    await waitFor(() => expect(crumbs).toHaveTextContent("Redwood Coast"));
    expect(within(crumbs).getByRole("link", { name: "Organization" })).toHaveAttribute(
      "href",
      "/map",
    );
    await findMarker(/^Pod Pod 01 · Alder Creek/);
  });
});

describe("the deployment layer", () => {
  afterEach(() => window.localStorage.clear());

  it("draws Pods as squares with their roll-up, and lists an unplaced Pod", async () => {
    actAs("owner");
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const ridge = await findMarker(/^Pod Pod 02 · Ridge Line/);
    expect(ridge.getAttribute("aria-label")).toBe("Pod Pod 02 · Ridge Line, Offline, 5 listeners");
    expect(ridge.querySelector(".map-marker-pod")).not.toBeNull();
    const alder = marker(/^Pod Pod 01 · Alder Creek/);
    expect(alder.querySelector(".map-nogps-badge")).toHaveTextContent("4 no GPS");
    const panel = screen.getByTestId("map-unplaced");
    expect(panel).toHaveTextContent("Pod 03 · Tarn Meadow");
    expect(within(panel).getByRole("link", { name: "Set location" })).toHaveAttribute(
      "href",
      expect.stringContaining("/inventory/pods/"),
    );
  });

  it("dims what is fine under the Alerts lens", async () => {
    actAs("owner");
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}`);
    await findMarker(/^Pod Pod 02 · Ridge Line/);
    await userEvent.click(screen.getByRole("tab", { name: "Alerts" }));
    await waitFor(() =>
      expect(marker(/^Pod Pod 02 · Ridge Line/).querySelector(".map-marker-dimmed")).not.toBeNull(),
    );
    expect(marker(/^Pod Pod 01 · Alder Creek/).querySelector(".map-marker-dimmed")).toBeNull();
    expect(screen.getByTestId("map-tab-note")).toHaveTextContent("E7");
  });

  it("drills to a Pod, whose crumb trail names the Deployment and the Pod", async () => {
    actAs("owner");
    renderAt(`/map/pods/${FIXTURE_IDS.alderCreekPod}`);
    const crumbs = await screen.findByRole("navigation", { name: "Hierarchy" });
    await waitFor(() => expect(crumbs).toHaveTextContent("Pod 01 · Alder Creek"));
    expect(within(crumbs).getByRole("link", { name: "Redwood Coast" })).toHaveAttribute(
      "href",
      `/map/deployments/${FIXTURE_IDS.redwoodCoast}`,
    );
  });

  it("says so for a Deployment outside scope or gone", async () => {
    actAs("owner");
    renderAt("/map/deployments/00000000-0000-4000-8000-00000000dead");
    expect(await screen.findByTestId("map-deployment-missing")).toBeInTheDocument();
  });

  it("clusters Listener pins, the cluster showing the worst status inside it", async () => {
    actAs("owner");
    renderAt(`/map/pods/${FIXTURE_IDS.alderCreekPod}`);
    await findMarker(/^Pod Pod 01 · Alder Creek/);
    // jsdom has no size, so the view falls back to a wide zoom where all nine
    // placed Listeners (4 + 3 + 2) share one cluster. One of them is
    // alerting, so the cluster is too (the server's severity order).
    await waitFor(() => expect(document.querySelector(".map-cluster")).not.toBeNull());
    const cluster = document.querySelector(".map-cluster")!;
    expect(cluster).toHaveAttribute("data-status", "alerting");
    expect(cluster).toHaveTextContent("9");
  });

  it("opens the selected Listener from a deep link", async () => {
    actAs("owner");
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}?device=listener:${ALERTING_MAC}`);
    const card = await screen.findByTestId("map-selected");
    expect(card).toHaveTextContent("alder-creek-03");
    expect(card).toHaveTextContent(ALERTING_MAC);
    await userEvent.click(within(card).getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("map-selected")).toBeNull();
  });
});
