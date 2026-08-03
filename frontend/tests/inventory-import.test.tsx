/**
 * Gate 27: the import screen (task E1.8 over E1.6) — dry-run-first flow,
 * non-status outcome rendering, the structurally-gated partial commit, the
 * auto-suffix checkbox mapping to the query param, and the viewer fallback.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

const DRY_RUN_REPORT = {
  committed: false,
  created: 0,
  failed: 1,
  rows: [
    { row: 1, status: "created", entity_id: "02:EE:0E:09:00:01", name: "imp-1", error: null },
    {
      row: 2,
      status: "error",
      entity_id: null,
      name: null,
      error: { code: "validation_error", message: "invalid MAC address 'nope'" },
    },
  ],
};

describe("import screen", () => {
  it("runs the dry-run flow and gates the partial commit on the checkbox", async () => {
    actAsOwner();
    const calls: Array<{ partial: string | null; auto_suffix: string | null }> = [];
    server.use(
      http.post("http://api.test/api/v1/listeners/import", ({ request }) => {
        const url = new URL(request.url);
        calls.push({
          partial: url.searchParams.get("partial"),
          auto_suffix: url.searchParams.get("auto_suffix"),
        });
        const partial = url.searchParams.get("partial") === "true";
        return HttpResponse.json(
          partial ? { ...DRY_RUN_REPORT, committed: true, created: 1 } : DRY_RUN_REPORT,
        );
      }),
    );
    renderAt("/inventory/import");
    await userEvent.click(await screen.findByLabelText(/Auto-suffix colliding names/));
    await userEvent.type(
      screen.getByLabelText("File contents"),
      "mac,name,aggregator_uuid,gps_lat,gps_lon,tags",
    );
    await userEvent.click(screen.getByRole("button", { name: /Validate & import/ }));

    const results = await screen.findByTestId("import-results");
    expect(results).toHaveTextContent("Nothing imported");
    const rows = within(screen.getByTestId("import-rows")).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("valid");
    expect(rows[1]).toHaveTextContent("validation_error");
    expect(rows[1]).toHaveClass("row-invalid");
    // No StatusChip / no device-state glyphs for row outcomes.
    expect(results.querySelectorAll("[data-status]")).toHaveLength(0);

    const commit = screen.getByTestId("commit-partial");
    expect(commit).toBeDisabled(); // structurally gated on the explicit checkbox
    await userEvent.click(screen.getByTestId("accept-partial"));
    expect(commit).toBeEnabled();
    await userEvent.click(commit);
    expect(await screen.findByText("Imported")).toBeInTheDocument();

    expect(calls).toEqual([
      { partial: "false", auto_suffix: "true" },
      { partial: "true", auto_suffix: "true" },
    ]);
  });

  it("a viewer gets the gated fallback, not the form", async () => {
    server.use(
      http.get("http://api.test/api/v1/auth/me", () =>
        HttpResponse.json({
          ...mePayload,
          assignments: [{ role: "viewer", deployment_id: null }],
        }),
      ),
    );
    renderAt("/inventory/import");
    expect(await screen.findByTestId("import-denied")).toBeInTheDocument();
    expect(screen.queryByTestId("import-form")).not.toBeInTheDocument();
  });
});
