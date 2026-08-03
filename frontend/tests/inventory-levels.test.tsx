/**
 * Gate 27: level pages (task E1.8) — server-driven sorting hits the D7 wire
 * grammar, footer captions, mono identifier cells, breadcrumb links, and the
 * v2 table treatment landing where the mockups put it.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { applyListParams, deployments, FIXTURE_IDS } from "./inventory-fixture";
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

describe("level pages", () => {
  it("header sort click flips to the -name wire grammar", async () => {
    actAsOwner();
    const sorts: string[] = [];
    server.use(
      http.get("http://api.test/api/v1/deployments", ({ request }) => {
        const url = new URL(request.url);
        sorts.push(url.searchParams.get("sort") ?? "");
        return HttpResponse.json(applyListParams(deployments, url));
      }),
    );
    renderAt("/inventory");
    const table = await screen.findByTestId("deployments-table");
    await userEvent.click(within(table).getByRole("button", { name: /Name/ }));
    // TanStack toggles asc -> desc; the page serializes to the D7 grammar.
    await userEvent.click(within(table).getByRole("button", { name: /Name/ }));
    expect(sorts).toContain("-name");
  });

  it("shows the caption, mono cells, and crumb links at deployment level", async () => {
    actAsOwner();
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    expect(await screen.findByRole("heading", { name: "Redwood Coast" })).toBeInTheDocument();
    const table = await screen.findByTestId("pods-table");
    expect(within(table).getAllByText("demo-agg-rc-01")[0].closest("td")).toHaveClass("cell-mono");
    expect(screen.getByText(/3 of 3 shown · sorted by name/)).toBeInTheDocument();
    // Breadcrumb: ancestor is a real link, final crumb is not (D25/E1.8).
    const crumbs = screen.getByRole("navigation", { name: "Hierarchy" });
    expect(within(crumbs).getByRole("link", { name: "Earth Echoes Demo" })).toHaveAttribute(
      "href",
      "/inventory",
    );
    expect(within(crumbs).getByText("Redwood Coast").getAttribute("aria-current")).toBe("page");
  });

  it("pod level lists listeners with MAC mono cells and the aggregator card", async () => {
    actAsOwner();
    renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    expect(
      await screen.findByRole("heading", { name: "Pod 01 · Alder Creek" }),
    ).toBeInTheDocument();
    const card = screen.getByTestId("aggregator-card");
    expect(within(card).getByText("demo-agg-rc-01")).toBeInTheDocument();
    const table = await screen.findByTestId("listeners-table");
    expect(within(table).getByText("02:EE:0E:01:01:01").closest("td")).toHaveClass("cell-mono");
    expect(screen.getByText(/8 of 8 shown/)).toBeInTheDocument();
  });

  it("listener detail shows identity, placement, and the epic-honest footer", async () => {
    actAsOwner();
    renderAt(`/inventory/listeners/${FIXTURE_IDS.firstListenerMac}`);
    expect(await screen.findByRole("heading", { name: "alder-creek-01" })).toBeInTheDocument();
    expect(screen.getByTestId("listener-mac")).toHaveTextContent("02:EE:0E:01:01:01");
    expect(screen.getByTestId("listener-facts")).toHaveTextContent("Live status arrives with E3");
  });
});
