/**
 * Gate 4 component checks (task E0.4): shell regions, routing including the
 * 404 view, and TanStack Query resolving against MSW with loading and error
 * states.
 */
import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { server } from "./msw-server";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("shell and routing", () => {
  // D25 restructured the shell from a left sidebar to a dark top bar; the
  // regions are the same two, under their new names.
  it("renders top bar and content regions", () => {
    renderAt("/");
    expect(screen.getByTestId("shell-topbar")).toBeInTheDocument();
    expect(screen.getByTestId("shell-content")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  });

  it("renders each declared route", async () => {
    for (const [path, heading] of [
      // "/" heading changed with the E1.8 Overview roll-up (V2·S1 title;
      // v2 wins on values — recorded with project-changes #16).
      ["/", "Organization overview"],
      ["/system", "System"],
      ["/inventory", "Inventory"],
      // E1.8 nested inventory routes (fixture ids from inventory-fixture.ts).
      ["/inventory/deployments/d1000000-0000-4000-8000-000000000001", "Redwood Coast"],
      ["/inventory/pods/b1100000-0000-4000-8000-000000000001", "Pod 01 · Alder Creek"],
      ["/inventory/listeners/02:EE:0E:01:01:01", "alder-creek-01"],
      ["/inventory/import", "Bulk import"],
      // E2.7: the live editor replaced the E1 shell; every level answers
      // the same "Configuration" title with the entity in the crumb.
      ["/configuration", "Configuration"],
      ["/configuration/pods/b1100000-0000-4000-8000-000000000001", "Configuration"],
      ["/configuration/listeners/02:EE:0E:01:01:01", "Configuration"],
      ["/provisioning", "Provisioning"],
    ]) {
      renderAt(path);
      expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
      cleanup();
    }
    renderAt("/map");
    expect(await screen.findByTestId("map-region")).toBeInTheDocument();
  });

  // The legend is the accessibility contract for the whole status vocabulary:
  // six states, each carrying a glyph as well as a color.
  it("renders all six device statuses in the map legend", async () => {
    renderAt("/map");
    const legend = await screen.findByTestId("status-legend");
    const statuses = [...legend.querySelectorAll("[data-status]")].map(
      (element) => element.getAttribute("data-status") ?? "",
    );
    expect(statuses).toEqual(["healthy", "sleeping", "degraded", "offline", "alerting", "drifted"]);
  });

  it("renders the 404 view for unknown routes", () => {
    renderAt("/no-such-page");
    expect(screen.getByTestId("not-found")).toBeInTheDocument();
  });
});

describe("query integration against MSW", () => {
  it("shows loading, then data from the mocked API", async () => {
    renderAt("/system");
    expect(screen.getByTestId("health-loading")).toBeInTheDocument();
    expect(await screen.findByTestId("health-data")).toBeInTheDocument();
    expect(screen.getByText("msw-fixture")).toBeInTheDocument();
  });

  it("renders the error state when the API fails", async () => {
    server.use(
      http.get("http://api.test/api/v1/health", () =>
        HttpResponse.json({ error: {} }, { status: 500 }),
      ),
    );
    renderAt("/system");
    expect(await screen.findByTestId("health-error")).toBeInTheDocument();
  });
});
