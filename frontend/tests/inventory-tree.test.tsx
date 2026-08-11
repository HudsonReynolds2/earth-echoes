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

  it("HONESTY GUARD (rewritten by E3.12/D60): status renders only where it is real", async () => {
    // D40 forbade every [data-status] because E1 and E2 had nothing real to
    // put in one. E3 supplies the real signals, so the guard is REPLACED
    // rather than deleted (D60): a chip may render only for a device whose
    // status the API actually reported, and a device that has never spoken
    // must render no chip at all.
    actAsOwner();
    const { container } = renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    // Wait for the LISTENER ROWS, not just the rail: the rail resolves before
    // the table has data, and a guard that passed on an empty page would be
    // asserting nothing at all.
    await screen.findByText("alder-creek-01");

    const chips = Array.from(container.querySelectorAll("[data-status]"));
    // The fixture gives exactly one listener a real status; the rest are
    // "unknown" and the aggregator has never reported.
    expect(chips).toHaveLength(1);
    expect(chips[0].getAttribute("data-status")).toBe("sleeping");
    // And "unknown" is never dressed as one of the six real states.
    expect(
      chips.some((chip) => chip.getAttribute("data-status") === "unknown"),
      "unknown must not render as a status chip",
    ).toBe(false);
    cleanup();
  });

  it("HONESTY GUARD: the config routes still show no status at all", async () => {
    // The part of D40 that still applies: E2's surfaces describe configuration,
    // not devices, and a status dot there would be decoration.
    actAsOwner();
    const { container } = renderAt(`/configuration/pods/${FIXTURE_IDS.alderCreekPod}`);
    await screen.findByRole("heading", { level: 1 });
    expect(container.querySelectorAll("[data-status]")).toHaveLength(0);
    cleanup();
  });
});
