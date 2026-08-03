/**
 * Gate 27: the Overview roll-up (task E1.8; project-changes #16) — real
 * E1-owned numbers only: the hero equals the listeners envelope total, cards
 * carry real counts, the attention slot names its epics, the first-run
 * variant offers the S7 dual actions, and no [data-status] renders.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
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

describe("organization overview", () => {
  it("hero and meta line carry the real envelope totals; no fabricated status", async () => {
    actAsOwner();
    const { container } = renderAt("/");
    const hero = await screen.findByTestId("overview-hero");
    // 28 listeners in the fixture (8+5+3+6+4+2) — the D7 envelope total.
    expect(hero).toHaveTextContent("Listeners registered");
    expect(hero).toHaveTextContent("28");
    expect(screen.getByTestId("overview-meta")).toHaveTextContent(
      "2 deployments · 6 pods · 28 listeners",
    );
    const cards = screen.getByTestId("overview-deployments");
    const redwood = within(cards).getByRole("heading", { name: "Redwood Coast" });
    expect(redwood.closest("section")).toHaveTextContent("3 pods · 16 listeners");
    expect(redwood.closest("section")).toHaveTextContent("Device status arrives with E3");
    expect(screen.getByTestId("overview-attention")).toHaveTextContent("E3");
    expect(container.querySelectorAll("[data-status]")).toHaveLength(0);
  });

  it("first-run empty state offers the S7 dual actions", async () => {
    actAsOwner();
    const empty = { items: [], total: 0, limit: 50, offset: 0 };
    server.use(
      http.get("http://api.test/api/v1/deployments", () => HttpResponse.json(empty)),
      http.get("http://api.test/api/v1/pods", () => HttpResponse.json(empty)),
      http.get("http://api.test/api/v1/listeners", () => HttpResponse.json(empty)),
    );
    renderAt("/");
    const emptyState = await screen.findByTestId("overview-empty");
    expect(emptyState).toHaveTextContent("A deployment groups pods around one telemetry stack");
    expect(within(emptyState).getByRole("link", { name: "Import inventory CSV" })).toHaveAttribute(
      "href",
      "/inventory/import",
    );
  });
});
