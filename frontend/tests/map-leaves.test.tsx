/**
 * E6.3: GPS-less Listeners as leaves (spec 9.2). On a Pod's route its
 * GPS-less Listeners fan out around it, are named as not surveyed, and are
 * drawn hollow; the toggle hides them; an unplaced Pod counts its GPS-less
 * Listeners; and a Listener that gains coordinates stops being a leaf on the
 * next read, which is what every inventory edit triggers in the same tab.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mapDeploymentFixture } from "./map-fixture";
import { mePayload, server } from "./msw-server";

function renderAt(
  path: string,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
  render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
  return client;
}

function leaves(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(".leaflet-marker-pane [aria-label]")].filter(
    (element) => /no GPS, drawn beside its pod$/.test(element.getAttribute("aria-label") ?? ""),
  );
}

describe("leaves", () => {
  afterEach(() => window.localStorage.clear());

  it("fan a Pod's GPS-less Listeners around it on its own route", async () => {
    renderAt(`/map/pods/${FIXTURE_IDS.alderCreekPod}`);
    // Alder Creek has 8 Listeners; odd indices (4 of them) have no GPS.
    await waitFor(() => expect(leaves()).toHaveLength(4));
    const names = leaves().map((leaf) => leaf.getAttribute("aria-label"));
    expect(names[0]).toBe("Listener alder-creek-02, No status yet, no GPS, drawn beside its pod");
    expect(leaves()[0].querySelector(".map-marker-leaf")).not.toBeNull();
    // Tethers are drawn, one per leaf.
    expect(document.querySelectorAll(".map-marker-leaf .map-tether")).toHaveLength(4);
  });

  it("hide with the toggle, and the choice is remembered", async () => {
    renderAt(`/map/pods/${FIXTURE_IDS.alderCreekPod}`);
    await waitFor(() => expect(leaves()).toHaveLength(4));
    await userEvent.click(within(screen.getByTestId("map-leaf-toggle")).getByRole("checkbox"));
    await waitFor(() => expect(leaves()).toHaveLength(0));
    expect(document.querySelectorAll(".map-tether")).toHaveLength(0);
    expect(window.localStorage.getItem("eoe.map.showLeaves")).toBe("off");
  });

  it("are counted on an unplaced Pod, which has nothing to orbit", async () => {
    renderAt(`/map/deployments/${FIXTURE_IDS.redwoodCoast}`);
    const panel = await screen.findByTestId("map-unplaced");
    // Tarn Meadow: 3 Listeners, index 1 without GPS.
    expect(panel).toHaveTextContent("Pod 03 · Tarn Meadow · 3 listeners, 1 without GPS");
  });

  it("become pins once they have coordinates, on the next read", async () => {
    let gave = false;
    server.use(
      http.get(`http://api.test/api/v1/map/deployments/${FIXTURE_IDS.redwoodCoast}`, () => {
        const detail = mapDeploymentFixture(FIXTURE_IDS.redwoodCoast)!;
        if (gave) {
          const row = detail.listeners.find((l) => l.name === "alder-creek-02")!;
          row.gps_lat = 41.2215;
          row.gps_lon = -124.0125;
        }
        return HttpResponse.json(detail);
      }),
    );
    const client = renderAt(`/map/pods/${FIXTURE_IDS.alderCreekPod}`);
    await waitFor(() => expect(leaves()).toHaveLength(4));
    // What the inventory form's save does in this tab: invalidate, refetch.
    gave = true;
    await client.invalidateQueries();
    await waitFor(() => expect(leaves()).toHaveLength(3));
    expect(leaves().map((leaf) => leaf.getAttribute("aria-label"))).not.toContain(
      "Listener alder-creek-02, No status yet, no GPS, drawn beside its pod",
    );
  });
});
