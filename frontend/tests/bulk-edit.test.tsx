/**
 * Gate 38: E2.8 bulk edit (spec 5.2; D56, D58). The acceptances: checkbox
 * multiselect feeds the modal with explicit ids; COMMIT IS GATED on a
 * server preview of the exact current form (any change re-disables it);
 * no-op rows mute; the Status column and Offline-now figure are E3 slots
 * with zero [data-status]; apply surfaces draft revisions; selections save
 * to the rail and reopen BY REFERENCE.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { seedOverrides } from "./config-fixture";
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

const POD_PATH = `/inventory/pods/${FIXTURE_IDS.alderCreekPod}`;

function previewItem(mac: string, noOp = false) {
  return {
    target_type: "listener",
    target_id: mac,
    name: `alder-creek-${mac.slice(-2)}`,
    pod_id: FIXTURE_IDS.alderCreekPod,
    pod_name: "Pod 01 · Alder Creek",
    deployment_id: FIXTURE_IDS.redwoodCoast,
    changed_keys: noOp ? [] : ["logging.verbosity"],
    no_op: noOp,
    before: { "logging.verbosity": { value: "info", source: "default", source_entity_id: null } },
    after: {
      "logging.verbosity": {
        value: noOp ? "info" : "debug",
        source: noOp ? "default" : "listener",
        source_entity_id: noOp ? null : mac,
      },
    },
  };
}

beforeEach(() => {
  seedOverrides();
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("checkbox multiselect", () => {
  it("selects rows and opens the modal with explicit ids", async () => {
    actAsOwner();
    const user = userEvent.setup();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("listeners-table");
    expect(screen.queryByTestId("bulk-edit-open")).toBeNull();
    await user.click(within(table).getByLabelText("Select alder-creek-01"));
    await user.click(within(table).getByLabelText("Select alder-creek-03"));
    const open = screen.getByTestId("bulk-edit-open");
    expect(open.textContent).toContain("Bulk edit (2)");
    await user.click(open);
    const modal = await screen.findByTestId("bulk-edit-modal");
    expect(modal.textContent).toContain("2 selected in Pod 01 · Alder Creek");
  });

  it("select-all toggles the visible page", async () => {
    actAsOwner();
    const user = userEvent.setup();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("listeners-table");
    await user.click(within(table).getByLabelText("Select all listeners on this page"));
    expect(screen.getByTestId("bulk-edit-open").textContent).toContain("Bulk edit (8)");
    await user.click(within(table).getByLabelText("Select all listeners on this page"));
    expect(screen.queryByTestId("bulk-edit-open")).toBeNull();
  });
});

describe("commit gating (the acceptance)", () => {
  async function openModalWithSelection(user: ReturnType<typeof userEvent.setup>) {
    renderAt(POD_PATH);
    const table = await screen.findByTestId("listeners-table");
    await user.click(within(table).getByLabelText("Select alder-creek-01"));
    await user.click(screen.getByTestId("bulk-edit-open"));
    return screen.findByTestId("bulk-edit-modal");
  }

  it("commit stays disabled until preview matches the CURRENT form", async () => {
    actAsOwner();
    const previews: unknown[] = [];
    server.use(
      http.post("http://api.test/api/v1/config/preview", async ({ request }) => {
        previews.push(await request.json());
        return HttpResponse.json({
          items: [previewItem("02:EE:0E:01:01:01")],
          total: 1,
          limit: 500,
          offset: 0,
        });
      }),
    );
    const user = userEvent.setup();
    const modal = await openModalWithSelection(user);

    const commit = within(modal).getByTestId("commit-change");
    expect(commit).toBeDisabled();
    expect(within(modal).getByTestId("preview-stale").textContent).toContain("Run Preview");

    await user.selectOptions(within(modal).getByLabelText("Setting"), "logging.verbosity");
    await user.selectOptions(within(modal).getByLabelText("Value for logging.verbosity"), "debug");
    expect(commit).toBeDisabled(); // form filled, still unpreviewed
    await user.click(within(modal).getByTestId("run-preview"));
    await waitFor(() => expect(within(modal).getByTestId("commit-change")).toBeEnabled());
    expect(previews).toHaveLength(1);

    // ANY change after preview re-disables until re-preview.
    await user.selectOptions(within(modal).getByLabelText("Value for logging.verbosity"), "trace");
    expect(within(modal).getByTestId("commit-change")).toBeDisabled();
    expect(within(modal).getByTestId("preview-stale").textContent).toContain(
      "changed since the last preview",
    );
  });

  it("previews honestly: no-op rows mute, the Status column is an E3 slot", async () => {
    actAsOwner();
    server.use(
      http.post("http://api.test/api/v1/config/preview", () =>
        HttpResponse.json({
          items: [previewItem("02:EE:0E:01:01:01"), previewItem("02:EE:0E:01:01:02", true)],
          total: 2,
          limit: 500,
          offset: 0,
        }),
      ),
    );
    const user = userEvent.setup();
    const modal = await openModalWithSelection(user);
    await user.selectOptions(within(modal).getByLabelText("Setting"), "logging.verbosity");
    await user.selectOptions(within(modal).getByLabelText("Value for logging.verbosity"), "debug");
    await user.click(within(modal).getByTestId("run-preview"));
    const preview = await within(modal).findByTestId("bulk-preview-table");

    const grid = within(modal).getByTestId("impact-grid");
    expect(grid.textContent).toContain("Matched");
    expect(grid.textContent).toContain("Will change");
    expect(grid.textContent).toContain("Offline now");
    expect(grid.textContent).toContain("live status arrives with E3");

    const rows = within(preview).getAllByRole("row").slice(1);
    expect(rows[0].className).not.toContain("row-noop");
    expect(rows[1].className).toContain("row-noop");
    expect(modal.querySelectorAll("[data-status]")).toHaveLength(0);
  });

  it("commit applies the previewed payload and shows draft revisions", async () => {
    actAsOwner();
    const applies: unknown[] = [];
    server.use(
      http.post("http://api.test/api/v1/config/preview", () =>
        HttpResponse.json({
          items: [previewItem("02:EE:0E:01:01:01")],
          total: 1,
          limit: 500,
          offset: 0,
        }),
      ),
      http.post("http://api.test/api/v1/config/apply", async ({ request }) => {
        applies.push(await request.json());
        return HttpResponse.json({
          state: "draft",
          publish_enabled: false,
          revisions: [
            {
              revision_id: "22222222-0000-4000-8000-000000000001",
              target_type: "listener",
              target_id: "02:EE:0E:01:01:01",
              deployment_id: FIXTURE_IDS.redwoodCoast,
              changed_keys: ["logging.verbosity"],
              checksum: "sha256:feedfacecafebeef",
            },
          ],
        });
      }),
    );
    const user = userEvent.setup();
    const modal = await openModalWithSelection(user);
    await user.selectOptions(within(modal).getByLabelText("Setting"), "logging.verbosity");
    await user.selectOptions(within(modal).getByLabelText("Value for logging.verbosity"), "debug");
    await user.click(within(modal).getByTestId("run-preview"));
    await waitFor(() => expect(within(modal).getByTestId("commit-change")).toBeEnabled());
    await user.click(within(modal).getByTestId("commit-change"));

    const outcome = await within(modal).findByTestId("apply-outcome");
    expect(outcome.textContent).toContain("1 draft revision");
    expect(outcome.textContent).toContain("draft");
    expect(outcome.textContent).toContain("E3");
    expect(applies).toHaveLength(1);
    expect(applies[0]).toEqual({
      selection: {
        entity_type: "listener",
        where: { ids: ["02:EE:0E:01:01:01"] },
      },
      changes: { "logging.verbosity": "debug" },
      level: "target",
    });
  });
});

describe("saved selections", () => {
  it("saves from the modal, lists in the config rail, reopens BY REFERENCE", async () => {
    actAsOwner();
    const created: unknown[] = [];
    server.use(
      http.post("http://api.test/api/v1/config/preview", () =>
        HttpResponse.json({
          items: [previewItem("02:EE:0E:01:01:01")],
          total: 1,
          limit: 500,
          offset: 0,
        }),
      ),
      http.post("http://api.test/api/v1/selections", async ({ request }) => {
        const body = (await request.json()) as { name: string; query: unknown };
        created.push(body);
        return HttpResponse.json(
          {
            id: "33333333-0000-4000-8000-000000000001",
            name: body.name,
            query: body.query,
            created_by: null,
            created_at: "2026-08-04T12:00:00Z",
          },
          { status: 201 },
        );
      }),
    );
    const user = userEvent.setup();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("listeners-table");
    await user.click(within(table).getByLabelText("Select alder-creek-01"));
    await user.click(screen.getByTestId("bulk-edit-open"));
    const modal = await screen.findByTestId("bulk-edit-modal");
    await user.type(within(modal).getByLabelText("Save as selection"), "alder one");
    await user.click(within(modal).getByRole("button", { name: "Save" }));
    await screen.findByText("Saved as “alder one”.");
    expect(created).toHaveLength(1);
  });

  it("the config rail lists saved selections and opens the modal by id", async () => {
    actAsOwner();
    const previews: unknown[] = [];
    server.use(
      http.get("http://api.test/api/v1/selections", () =>
        HttpResponse.json({
          items: [
            {
              id: "33333333-0000-4000-8000-000000000001",
              name: "coastal listeners",
              query: { entity_type: "listener", where: { tag: "coastal" } },
              created_by: null,
              created_at: "2026-08-04T12:00:00Z",
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      ),
      http.post("http://api.test/api/v1/config/preview", async ({ request }) => {
        previews.push(await request.json());
        return HttpResponse.json({
          items: [previewItem("02:EE:0E:01:01:01")],
          total: 1,
          limit: 500,
          offset: 0,
        });
      }),
    );
    const user = userEvent.setup();
    renderAt("/configuration");
    const rail = await screen.findByTestId("saved-selections");
    await user.click(await within(rail).findByRole("button", { name: "coastal listeners" }));
    const modal = await screen.findByTestId("bulk-edit-modal");
    expect(modal.textContent).toContain("coastal listeners");

    await user.selectOptions(within(modal).getByLabelText("Setting"), "logging.verbosity");
    await user.selectOptions(within(modal).getByLabelText("Value for logging.verbosity"), "debug");
    await user.click(within(modal).getByTestId("run-preview"));
    await waitFor(() => expect(previews).toHaveLength(1));
    // BY REFERENCE: the server re-evaluates membership at use (D54).
    expect((previews[0] as { selection: unknown }).selection).toEqual({
      selection_id: "33333333-0000-4000-8000-000000000001",
    });
  });

  it("shows the empty state when nothing is saved", async () => {
    actAsOwner();
    renderAt("/configuration");
    const rail = await screen.findByTestId("saved-selections");
    expect(rail.textContent).toContain("None yet");
  });
});
