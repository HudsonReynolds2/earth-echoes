/**
 * Gate 27: the hierarchy tree (task E1.8) — structure, counts, filtering,
 * route-tracking selection, mono aggregator labels, and the honesty guard:
 * NO [data-status] anywhere on inventory routes until E3 supplies reported
 * state (owner decision: no fabricated status).
 */
import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";

function actAsOwner() {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("hierarchy tree", () => {
  it("renders org, deployments, pods, and mono aggregator rows with counts", async () => {
    actAsOwner();
    renderAt("/inventory");
    const rail = await screen.findByTestId("tree-rail");
    expect(await within(rail).findByText("Earth Echoes Demo")).toBeInTheDocument();
    expect(within(rail).getByText("Redwood Coast")).toBeInTheDocument();
    expect(within(rail).getByText("High Desert")).toBeInTheDocument();
    expect(within(rail).getByText("Pod 01 · Alder Creek")).toBeInTheDocument();
    const aggLabel = within(rail).getByText("demo-agg-rc-01");
    expect(aggLabel.closest("[data-kind]")?.getAttribute("data-kind")).toBe("aggregator");
    // Right-aligned counts: Redwood Coast has 3 pods, Alder Creek 8 listeners.
    const redwood = within(rail).getByText("Redwood Coast").closest("a");
    expect(redwood?.querySelector(".tree-count")?.textContent).toBe("3");
  });

  it("filter narrows rows while keeping matching branches", async () => {
    actAsOwner();
    renderAt("/inventory");
    const rail = await screen.findByTestId("tree-rail");
    await within(rail).findByText("High Desert");
    await userEvent.type(within(rail).getByLabelText("Filter hierarchy"), "alder");
    expect(within(rail).queryByText("High Desert")).not.toBeInTheDocument();
    // The matching pod's ancestors stay visible.
    expect(within(rail).getByText("Redwood Coast")).toBeInTheDocument();
    expect(within(rail).getByText("Pod 01 · Alder Creek")).toBeInTheDocument();
  });

  it("selection tracks the route via aria-current", async () => {
    actAsOwner();
    renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    const rail = await screen.findByTestId("tree-rail");
    const selected = await within(rail).findByText("Pod 01 · Alder Creek");
    expect(selected.closest("a")?.getAttribute("aria-current")).toBe("page");
  });

  it("HONESTY GUARD: no [data-status] renders on any inventory route", async () => {
    actAsOwner();
    for (const path of [
      "/inventory",
      `/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`,
      `/inventory/pods/${FIXTURE_IDS.alderCreekPod}`,
      `/inventory/listeners/${FIXTURE_IDS.firstListenerMac}`,
    ]) {
      const { container } = renderAt(path);
      await screen.findByTestId("tree-rail");
      expect(
        container.querySelectorAll("[data-status]"),
        `fabricated status on ${path}`,
      ).toHaveLength(0);
      cleanup();
    }
  });
});
