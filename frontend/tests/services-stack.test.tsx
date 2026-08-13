/**
 * Services onboarding, Path B and the status display (task E5.12b; spec 16.3,
 * 16.5, screen S5).
 *
 * What these assert that the Path A suite does not:
 *
 * * The generated-stack path — generate, download, rotate — including the two
 *   outcomes that look like bugs and are not: a generated stack is `untested`,
 *   and a rotation whose re-verification FAILS still published.
 * * The rollup is rendered in its own four-value vocabulary, never as a
 *   service's three-value one.
 * * The spec 16.5 gate: a provisioning bundle needs a verified broker, and the
 *   UI warns when the rest are not verified.
 * * A viewer and a field tech see status and no Generate.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";
import { seedServices, serviceStore } from "./services-fixture";

const CONFIGURED = FIXTURE_IDS.redwoodCoast;
/** Nothing configured: the deployment that opens on Path B. */
const FRESH = FIXTURE_IDS.highDesert;
const API = "http://api.test/api/v1";

function actAs(role: string) {
  server.use(
    http.get(`${API}/auth/me`, () =>
      HttpResponse.json({
        ...mePayload,
        assignments: [{ role, deployment_id: role === "owner" ? null : FRESH }],
      }),
    ),
  );
}

function renderAt(deploymentId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[`/inventory/deployments/${deploymentId}/services`]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  seedServices();
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("path selection", () => {
  it("opens a deployment with nothing configured on Path B", async () => {
    actAs("owner");
    renderAt(FRESH);
    expect(await screen.findByTestId("stack-panel")).toBeInTheDocument();
  });

  it("opens a deployment that already has services on Path A", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    await screen.findByTestId("service-mqtt");
    expect(screen.queryByTestId("stack-panel")).toBeNull();
  });

  it("switches between the two paths, keeping the verify cards on both", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    await screen.findByTestId("service-mqtt");
    await userEvent.click(screen.getByRole("button", { name: /^Path B/ }));
    expect(await screen.findByTestId("stack-panel")).toBeInTheDocument();
    // The five cards are the verify step and belong to BOTH paths.
    expect(screen.getByTestId("service-mqtt")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^Path A/ }));
    expect(screen.queryByTestId("stack-panel")).toBeNull();
  });
});

describe("generating a stack", () => {
  it("sends the operator's choices and reports that nothing is verified yet", async () => {
    actAs("owner");
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post(`${API}/deployments/:id/services/stack`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          deployment_id: FRESH,
          services_status: "pending_verification",
          services: ["mqtt", "influx", "prometheus", "grafana"],
          download_path: `/deployments/${FRESH}/services/stack/download`,
        });
      }),
    );
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");

    await userEvent.clear(within(panel).getByLabelText("Hostname"));
    await userEvent.type(within(panel).getByLabelText("Hostname"), "kvm-01.example.org");
    await userEvent.type(within(panel).getByLabelText("IP address (optional)"), "10.0.0.4");
    await userEvent.click(within(panel).getByLabelText("Include object storage (optional)"));
    await userEvent.click(within(panel).getByRole("button", { name: "Generate stack" }));

    const generated = await within(panel).findByTestId("stack-generated");
    expect(body).toEqual({
      hostname: "kvm-01.example.org",
      ip: "10.0.0.4",
      include_object_storage: true,
    });
    // The acceptance that reads like a bug: a generated stack does NOT vouch
    // for itself. Every service is untested until the operator runs it.
    expect(generated).toHaveTextContent("untested");
    expect(generated).toHaveTextContent("pending_verification");
  });

  it("warns that the archive is a credential, before anything is downloaded", async () => {
    actAs("owner");
    renderAt(FRESH);
    const warning = await screen.findByTestId("stack-credential-warning");
    expect(warning).toHaveTextContent(/private key and every service password/);
    expect(warning).toHaveTextContent(/Treat it as a credential/);
  });

  it("surfaces a generation failure without pretending a stack exists", async () => {
    actAs("owner");
    server.use(
      http.post(`${API}/deployments/:id/services/stack`, () =>
        HttpResponse.json(
          { error: { code: "conflict", message: "a stack already exists", detail: null } },
          { status: 409 },
        ),
      ),
    );
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");
    await userEvent.click(within(panel).getByRole("button", { name: "Generate stack" }));
    expect(await within(panel).findByTestId("stack-error")).toHaveTextContent(
      "a stack already exists",
    );
    expect(within(panel).queryByTestId("stack-generated")).toBeNull();
  });
});

describe("downloading the bundle", () => {
  it("hands the bytes to the browser and says the download is reproducible", async () => {
    actAs("owner");
    const created: string[] = [];
    const revoked: string[] = [];
    // Patch the two METHODS, never the global — jsdom has no object-URL
    // support, but replacing `URL` itself takes the constructor with it and
    // every later test dies inside tough-cookie instead of where the fault is.
    const realCreate = URL.createObjectURL;
    const realRevoke = URL.revokeObjectURL;
    URL.createObjectURL = (blob: Blob) => {
      created.push(blob.type);
      return "blob:stack";
    };
    URL.revokeObjectURL = (url: string) => {
      revoked.push(url);
    };
    server.use(
      http.get(`${API}/deployments/:id/services/stack/download`, () =>
        HttpResponse.arrayBuffer(new Uint8Array([31, 139, 8]).buffer, {
          headers: {
            "Content-Type": "application/gzip",
            "Content-Disposition": 'attachment; filename="echoes-stack-high-desert.tar.gz"',
          },
        }),
      ),
    );
    try {
      renderAt(FRESH);
      const panel = await screen.findByTestId("stack-panel");
      await userEvent.click(within(panel).getByRole("button", { name: "Download bundle" }));

      const notice = await within(panel).findByTestId("stack-notice");
      expect(notice).toHaveTextContent("echoes-stack-high-desert.tar.gz");
      expect(notice).toHaveTextContent("byte-identical");
      expect(created).toHaveLength(1);
      // Nothing is held: the object URL is released in the same turn it is made.
      expect(revoked).toEqual(["blob:stack"]);
    } finally {
      URL.createObjectURL = realCreate;
      URL.revokeObjectURL = realRevoke;
    }
  });

  it("explains a 404 as 'no stack generated' rather than as a broken page", async () => {
    actAs("owner");
    server.use(
      http.get(`${API}/deployments/:id/services/stack/download`, () =>
        HttpResponse.json(
          {
            error: {
              code: "not_found",
              message: "no generated stack for this deployment",
              detail: null,
            },
          },
          { status: 404 },
        ),
      ),
    );
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");
    await userEvent.click(within(panel).getByRole("button", { name: "Download bundle" }));
    expect(await within(panel).findByTestId("stack-error")).toHaveTextContent(
      /has no generated stack/,
    );
  });
});

describe("rotation", () => {
  const rotateHandler = (verified: boolean, revisions: number) =>
    http.post(`${API}/deployments/:id/services/stack/rotate`, () =>
      HttpResponse.json({
        deployment_id: FRESH,
        services_status: verified ? "verified" : "degraded",
        revisions,
        verified,
        results: [
          { service_key: "mqtt", outcome: verified ? "pass" : "fail", checks: [] },
          { service_key: "influx", outcome: "pass", checks: [] },
        ],
        download_path: `/deployments/${FRESH}/services/stack/download`,
      }),
    );

  it("asks for confirmation before rotating every credential", async () => {
    actAs("owner");
    let called = 0;
    server.use(
      http.post(`${API}/deployments/:id/services/stack/rotate`, () => {
        called += 1;
        return HttpResponse.json({
          deployment_id: FRESH,
          services_status: "verified",
          revisions: 3,
          verified: true,
          results: [],
          download_path: "",
        });
      }),
    );
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");
    await userEvent.click(within(panel).getByTestId("stack-rotate"));
    expect(called).toBe(0);
    await userEvent.click(within(panel).getByRole("button", { name: "Cancel" }));
    expect(called).toBe(0);

    await userEvent.click(within(panel).getByTestId("stack-rotate"));
    await userEvent.click(
      within(panel).getByRole("button", { name: "Yes, rotate every credential" }),
    );
    await within(panel).findByTestId("stack-rotation-result");
    expect(called).toBe(1);
  });

  it("reports how many devices were told", async () => {
    actAs("owner");
    server.use(rotateHandler(true, 3));
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");
    await userEvent.click(within(panel).getByTestId("stack-rotate"));
    await userEvent.click(
      within(panel).getByRole("button", { name: "Yes, rotate every credential" }),
    );
    const result = await within(panel).findByTestId("stack-rotation-result");
    expect(result).toHaveTextContent("3 device configurations republished");
    expect(result).toHaveTextContent("Re-verification passed");
  });

  it("says a FAILED re-verification still published, and why", async () => {
    actAs("owner");
    server.use(rotateHandler(false, 2));
    renderAt(FRESH);
    const panel = await screen.findByTestId("stack-panel");
    await userEvent.click(within(panel).getByTestId("stack-rotate"));
    await userEvent.click(
      within(panel).getByRole("button", { name: "Yes, rotate every credential" }),
    );
    // The endpoint's inverted ordering, made visible: an operator who is not
    // told this reads a failure and assumes nothing shipped.
    const unverified = await within(panel).findByTestId("stack-rotation-unverified");
    expect(unverified).toHaveTextContent(/published anyway/);
    expect(unverified).toHaveTextContent(/degraded/);
    expect(within(panel).getByTestId("stack-rotation-result")).toHaveTextContent(
      "2 device configurations republished",
    );
  });
});

describe("the rolled-up status display", () => {
  it("renders the rollup's own four-value vocabulary, not a service's three", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    // Influx is `failed` in the fixture and it is required, so the deployment
    // rolls up to degraded — a word no per-service chip can ever carry.
    expect(await screen.findByTestId("services-rollup")).toHaveTextContent("degraded");
    // And it is not painted with the device StatusChip either.
    expect(document.querySelectorAll(".status-chip")).toHaveLength(0);
  });

  it("counts verified against REQUIRED, so an optional service cannot hold it back", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    const summary = await screen.findByTestId("services-summary");
    // mqtt verified, influx failed; s3 is unconfigured and therefore not
    // required, so the denominator is 4 and not 5.
    expect(within(summary).getByText("1/4")).toBeInTheDocument();
  });

  it("says degradation comes from observed events, not from a timer", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    const summary = await screen.findByTestId("services-summary");
    // D133 closed periodic re-checks as deliberately not built. The S5 mock's
    // "re-checks run every 5 minutes" would be a promise the platform does not
    // keep, so the page must not make it.
    expect(summary).toHaveTextContent(/Nothing re-checks these on a timer/);
    expect(summary.textContent).not.toMatch(/every \d+ minutes/);
  });
});

describe("the spec 16.5 provisioning gate", () => {
  it("is open when the broker is verified, and warns about the rest", async () => {
    actAs("owner");
    renderAt(CONFIGURED);
    const gate = await screen.findByTestId("services-gate");
    expect(gate).toHaveTextContent("Unblocked");
    // Influx is failed, so devices would have nowhere to ship analysis.
    expect(within(gate).getByTestId("services-gate-warning")).toHaveTextContent(
      /nowhere to ship analysis, metrics or audio/,
    );
  });

  it("is closed when the broker is not verified, and says why", async () => {
    actAs("owner");
    serviceStore.get(CONFIGURED)!.mqtt.status = "failed";
    renderAt(CONFIGURED);
    const gate = await screen.findByTestId("services-gate");
    expect(await within(gate).findByTestId("services-gate-closed")).toHaveTextContent(
      /requires a verified broker/,
    );
    expect(gate).not.toHaveTextContent("Unblocked");
  });

  it("drops the warning once every required service is verified", async () => {
    actAs("owner");
    serviceStore.get(CONFIGURED)!.influx.status = "verified";
    const rows = serviceStore.get(CONFIGURED)!;
    for (const key of ["prometheus", "grafana"]) {
      rows[key].configured = true;
      rows[key].status = "verified";
    }
    renderAt(CONFIGURED);
    const gate = await screen.findByTestId("services-gate");
    expect(gate).toHaveTextContent("Unblocked");
    expect(within(gate).queryByTestId("services-gate-warning")).toBeNull();
    expect(await screen.findByTestId("services-rollup")).toHaveTextContent("verified");
  });
});

describe("permissions on Path B (phase-5 fixed choice 9)", () => {
  it.each(["viewer", "field_tech"])("shows %s the status and no Generate", async (role) => {
    actAs(role);
    renderAt(FRESH);
    await screen.findByTestId("service-mqtt");
    // Status renders for every role — view_services reaches all four.
    expect(screen.getByTestId("services-summary")).toBeInTheDocument();
    expect(screen.getByTestId("services-gate")).toBeInTheDocument();
    // And the stack path is not reachable at all.
    expect(screen.queryByTestId("stack-panel")).toBeNull();
    expect(screen.queryByRole("button", { name: "Generate stack" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Download bundle" })).toBeNull();
    expect(screen.queryByTestId("stack-rotate")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Path B/ })).toBeNull();
  });
});
