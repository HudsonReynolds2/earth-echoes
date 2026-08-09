/**
 * Gate 37: the schema-driven config editor (task E2.7; S3; D50-D51).
 * The load-bearing acceptances: the TEST KEY renders a working editor with
 * zero frontend references (spec 5.3 "the catalog is data"); provenance
 * rows read quiet/loud correctly; the level rule renders below-lowest keys
 * read-only; staging drives the banner and diff and saves as ONE wholesale
 * PUT; 422 errors land on their named rows; and zero [data-status] elements
 * exist on config routes (the D40 guard extends here).
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS } from "./inventory-fixture";
import { seedOverrides } from "./config-fixture";
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

const POD_PATH = `/configuration/pods/${FIXTURE_IDS.alderCreekPod}`;

beforeEach(() => {
  seedOverrides();
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("provenance table", () => {
  it("renders every applicable key with quiet/loud provenance at pod level", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");

    // Overridden at this level: loud row, revert control present.
    const ssidRow = within(table).getByText("network.wifi_ssid").closest("tr")!;
    expect(ssidRow.className).toContain("row-overridden");
    expect(within(ssidRow).getByText("set here")).toBeInTheDocument();
    expect(
      within(ssidRow).getByRole("button", { name: "Remove override network.wifi_ssid" }),
    ).toBeInTheDocument();

    // Inherited from an ancestor: quiet, no revert, true source shown.
    const rateRow = within(table).getByText("audio.sample_rate_hz").closest("tr")!;
    expect(rateRow.className).not.toContain("row-overridden");
    expect(within(rateRow).getByText("inherited")).toBeInTheDocument();
    expect(within(rateRow).getByText("Deployment")).toBeInTheDocument();
    expect(within(rateRow).queryByRole("button", { name: /Remove override/ })).toBeNull();

    // Untouched: default chip, dashed treatment.
    const bitsRow = within(table).getByText("audio.bits_per_sample").closest("tr")!;
    expect(bitsRow.querySelector(".provenance-chip")).toHaveTextContent("default");

    // Inventory keys do NOT render at pod level (the merge omits them).
    expect(within(table).queryByText("identity.name")).toBeNull();
  });

  it("THE ACCEPTANCE: the test key renders a working editor from catalog data alone", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    // Nothing in src/ names test.demo_knob; the row, its group header, and
    // a working enum editor all come from the fixture catalog.
    expect(within(table).getByText("test.demo_knob")).toBeInTheDocument();
    const editor = within(table).getByRole("combobox", { name: "Value for test.demo_knob" });
    expect(
      within(editor)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["alpha", "beta", "gamma"]);
  });

  it("service keys are locked naming E5; below-lowest keys are read-only at listener", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    const influxRow = within(table).getByText("telemetry.influx_url").closest("tr")!;
    expect(within(influxRow).getByText(/arrives with E5/)).toBeInTheDocument();
    expect(within(influxRow).queryByRole("textbox")).toBeNull();
  });

  it("the level rule renders pod-lowest keys read-only at listener level", async () => {
    actAsOwner();
    renderAt(`/configuration/listeners/${FIXTURE_IDS.firstListenerMac}`);
    const table = await screen.findByTestId("provenance-table");
    const ssidRow = within(table).getByText("network.wifi_ssid").closest("tr")!;
    // Inherited from the pod, visible - but no editor at this level (D50).
    expect(within(ssidRow).getByText("inherited")).toBeInTheDocument();
    expect(within(ssidRow).queryByRole("textbox")).toBeNull();
    // And inventory keys resolve visibly, uneditable, from the listener row.
    const nameRow = within(table).getByText("identity.name").closest("tr")!;
    expect(within(nameRow).getByText("alder-creek-01")).toBeInTheDocument();
    expect(nameRow.querySelector(".provenance-chip")).toHaveTextContent("inventory");
  });
});

describe("draft staging and the one-PUT save", () => {
  it("stages, banners, diffs, and saves the exact recomputed sparse map once", async () => {
    actAsOwner();
    const user = userEvent.setup();
    const puts: unknown[] = [];
    server.use(
      http.put("http://api.test/api/v1/pods/:id/config/overrides", async ({ request, params }) => {
        const body = (await request.json()) as { overrides: Record<string, unknown> };
        puts.push(body);
        return HttpResponse.json({
          entity_type: "pod",
          entity_id: String(params.id),
          catalog_version: 4,
          overrides: body.overrides,
        });
      }),
    );
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    expect(screen.queryByTestId("draft-banner")).toBeNull();

    const verbosity = within(table).getByRole("combobox", {
      name: "Value for logging.verbosity",
    });
    await user.selectOptions(verbosity, "debug");
    const banner = await screen.findByTestId("draft-banner");
    expect(banner.textContent).toContain("1 key changed");
    expect(banner.textContent).toContain("Nothing reaches devices until you publish");

    // Revert the saved ssid override too: the PUT must DROP it.
    await user.click(screen.getByRole("button", { name: "Remove override network.wifi_ssid" }));
    expect((await screen.findByTestId("draft-banner")).textContent).toContain("2 keys changed");

    const diff = screen.getByTestId("draft-diff");
    expect(diff.textContent).toContain("logging.verbosity");
    expect(diff.textContent).toContain("info");
    expect(diff.textContent).toContain("debug");
    expect(diff.textContent).toContain("inherited again");

    await user.click(screen.getByTestId("save-draft"));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({
      overrides: {
        // The pod's surviving overrides + the staged edit; ssid reverted out.
        "network.wifi_password": { $secret_set: true },
        "capture.duty_on_seconds": 90,
        "logging.verbosity": "debug",
      },
    });
    await waitFor(() => expect(screen.queryByTestId("draft-banner")).toBeNull());
  });

  it("lands 422 errors on their named rows and stages nothing away", async () => {
    actAsOwner();
    const user = userEvent.setup();
    server.use(
      http.put("http://api.test/api/v1/pods/:id/config/overrides", () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "invalid override map",
              detail: {
                errors: [
                  {
                    key: "capture.duty_on_seconds",
                    code: "invalid_value",
                    message: "capture.duty_on_seconds: expected an integer",
                  },
                ],
              },
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    const duty = within(table).getByRole("spinbutton", {
      name: "Value for capture.duty_on_seconds",
    });
    await user.clear(duty);
    await user.type(duty, "120");
    await user.click(screen.getByTestId("save-draft"));
    const row = (await screen.findByText(/expected an integer/)).closest("tr")!;
    expect(within(row).getByText("capture.duty_on_seconds")).toBeInTheDocument();
    // Still staged - the draft survives a failed save.
    expect(screen.getByTestId("draft-banner")).toBeInTheDocument();
  });
});

describe("chrome and guards", () => {
  it("publishes nothing: the Publish button is disabled and names E3", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    const publish = screen.getByRole("button", { name: "Publish revision" });
    expect(publish).toBeDisabled();
    expect(publish.title).toContain("E3");
    expect(publish.title).toContain("EOE_PUBLISH_ENABLED");
  });

  it("keeps the D40 guard: zero [data-status] elements on config routes", async () => {
    actAsOwner();
    const { container } = renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    expect(container.querySelectorAll("[data-status]")).toHaveLength(0);
  });

  it("shows the footer catalog version and the inheritance chain", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    expect(screen.getByText(/catalog schema v4/)).toBeInTheDocument();
    const chain = screen.getByTestId("inheritance-chain");
    expect(within(chain).getByText("Pod 01 · Alder Creek")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(chain).getAllByText(/overrides?$/).length).toBeGreaterThanOrEqual(3),
    );
  });

  it("the Revisions tab lists per-device drafts and explains itself elsewhere", async () => {
    actAsOwner();
    renderAt(`${POD_PATH}?tab=Revisions`);
    expect(await screen.findByTestId("revisions-not-device")).toBeInTheDocument();

    server.use(
      http.get("http://api.test/api/v1/listeners/:mac/revisions", () =>
        HttpResponse.json({
          items: [
            {
              id: "11111111-0000-4000-8000-000000000001",
              target_type: "listener",
              target_id: FIXTURE_IDS.firstListenerMac,
              deployment_id: FIXTURE_IDS.redwoodCoast,
              schema_version: 1,
              checksum: "sha256:abcdef0123456789",
              state: "draft",
              created_by: null,
              created_at: "2026-08-04T12:00:00Z",
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
      ),
    );
    renderAt(`/configuration/listeners/${FIXTURE_IDS.firstListenerMac}?tab=Revisions`);
    const revisions = await screen.findByTestId("revisions-table");
    expect(within(revisions).getByText("draft")).toBeInTheDocument();
    expect(within(revisions).getByText(/sha256:/)).toBeInTheDocument();
  });
});
