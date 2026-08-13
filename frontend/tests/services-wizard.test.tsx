/**
 * Services onboarding, Path A (task E5.12a; spec 16.2, 16.3, screen S5).
 *
 * The assertions that carry the acceptance:
 *
 * * **A saved secret's input is EMPTY after a load.** This is the whole
 *   write-only claim, checked the only way that means anything — reading the
 *   input's value rather than trusting that nothing populated it.
 * * **`services_status` is not device status.** Its chip is not `StatusChip`,
 *   and the two vocabularies never share a word (D40, the DES three-channel
 *   rule).
 * * **A viewer and a field tech see status and no Save, Test or Generate.**
 * * A failing check renders its REMEDY, because E5.3 makes remedies
 *   mandatory precisely so this screen can show them.
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
import { seedServices } from "./services-fixture";

const DEPLOYMENT = FIXTURE_IDS.redwoodCoast;
const PATH = `/inventory/deployments/${DEPLOYMENT}/services`;
const API = "http://api.test/api/v1";

function actAs(role: string) {
  server.use(
    http.get(`${API}/auth/me`, () =>
      HttpResponse.json({
        ...mePayload,
        assignments: [{ role, deployment_id: role === "owner" ? null : DEPLOYMENT }],
      }),
    ),
  );
}

function renderAt(path: string = PATH) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

const card = async (key: string) => await screen.findByTestId(`service-${key}`);
/** One field's whole cell. Addressed by test id because a secret field in its
 * resting state has no input to find it by — which is the point of it. */
const field = async (service: string, name: string) =>
  await screen.findByTestId(`service-${service}-${name}-field`);

beforeEach(() => {
  seedServices();
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("the five forms are rendered from the schema", () => {
  it("renders all five services, configured or not, in spec 16.2 order", async () => {
    actAs("owner");
    renderAt();
    await card("mqtt");
    const headings = screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent);
    expect(headings).toEqual([
      "Mosquitto",
      "InfluxDB 3",
      "Prometheus",
      "Grafana",
      "Object storage",
    ]);
  });

  it("gives each service its own fields, with the model's required-ness", async () => {
    actAs("owner");
    renderAt();
    const prometheus = await card("prometheus");
    // Two URLs, because they are two endpoints with two roles (E5.4c).
    expect(within(prometheus).getByLabelText("Read URL")).toBeInTheDocument();
    expect(within(prometheus).getByLabelText("Remote-write URL")).toBeInTheDocument();
    // Optional fields say so; required ones do not carry the suffix.
    const s3 = await card("s3");
    expect(within(s3).getByLabelText("Bucket")).toBeRequired();
    expect(within(s3).getByLabelText("Region (optional)")).not.toBeRequired();
  });

  it("types a number field as a number and a boolean as a checkbox", async () => {
    actAs("owner");
    renderAt();
    const mqtt = await card("mqtt");
    expect(within(mqtt).getByLabelText("Port")).toHaveAttribute("type", "number");
    expect(within(mqtt).getByLabelText("TLS enabled (optional)")).toHaveAttribute(
      "type",
      "checkbox",
    );
  });

  it("populates non-secret fields from the stored row", async () => {
    actAs("owner");
    renderAt();
    const mqtt = await card("mqtt");
    expect(within(mqtt).getByLabelText("Host")).toHaveValue("kvm-01.example.org");
    expect(within(mqtt).getByLabelText("Port")).toHaveValue(8883);
    expect(within(mqtt).getByLabelText("Username")).toHaveValue("platform");
  });
});

describe("secrets are write-only in the UI as well as the API", () => {
  it("renders a stored secret as set-ness, with NO input holding a value", async () => {
    actAs("owner");
    renderAt();
    await card("mqtt");
    const cell = await field("mqtt", "password");

    expect(within(cell).getByText("set")).toBeInTheDocument();
    // The acceptance, literally: no input on this card is populated from the
    // response. Before Replace there is no input at all, and after it there is
    // an empty one.
    expect(within(cell).queryByLabelText("Password")).toBeNull();

    await userEvent.click(within(cell).getByRole("button", { name: "Replace" }));
    const input = within(cell).getByLabelText("Password");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("type", "password");
  });

  it("never renders the sentinel, or any credential-shaped value, as text", async () => {
    actAs("owner");
    renderAt();
    const mqtt = await card("mqtt");
    expect(mqtt.textContent).not.toContain("$secret_set");
    expect(mqtt.textContent).not.toContain("secret_set");
  });

  it("an unset secret offers its input directly, empty, with no Replace step", async () => {
    actAs("owner");
    renderAt();
    await card("grafana");
    const cell = await field("grafana", "service_account_token");
    expect(within(cell).getByText("not set")).toBeInTheDocument();
    expect(within(cell).getByLabelText(/^Service account token/)).toHaveValue("");
    expect(within(cell).queryByRole("button", { name: "Replace" })).toBeNull();
  });

  it("sends the keep sentinel for an untouched secret and plaintext for a replaced one", async () => {
    actAs("owner");
    let body: Record<string, unknown> | null = null;
    server.use(
      http.put(`${API}/deployments/:id/services`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ deployment_id: DEPLOYMENT, services: {} });
      }),
    );
    renderAt();
    const mqtt = await card("mqtt");

    // Save once with nothing touched: the password must round-trip as the
    // sentinel, or a form the operator never held the secret for would clear
    // the broker's credential on every save.
    await userEvent.click(within(mqtt).getByRole("button", { name: "Save" }));
    await screen.findByText("Mosquitto");
    const first = (body as unknown as { services: { mqtt: Record<string, unknown> } }).services
      .mqtt;
    expect(first.password).toEqual({ $secret_set: true });
    expect(first.host).toBe("kvm-01.example.org");
    // Wholesale, not a patch: every field the form owns is present.
    expect(Object.keys(first).sort()).toEqual(
      ["host", "password", "port", "tls_enabled", "username"].sort(),
    );

    // Replace it, and the plaintext goes instead of the sentinel.
    const cell = await field("mqtt", "password");
    await userEvent.click(within(cell).getByRole("button", { name: "Replace" }));
    await userEvent.type(within(cell).getByLabelText("Password"), "new-broker-secret");
    await userEvent.click(within(mqtt).getByRole("button", { name: "Save" }));

    const second = (body as unknown as { services: { mqtt: Record<string, unknown> } }).services
      .mqtt;
    expect(second.password).toBe("new-broker-secret");
  });

  it("keeps typed plaintext inside its own input and out of the DOM", async () => {
    actAs("owner");
    renderAt();
    const mqtt = await card("mqtt");
    const cell = await field("mqtt", "password");
    await userEvent.click(within(cell).getByRole("button", { name: "Replace" }));
    await userEvent.type(within(cell).getByLabelText("Password"), "hunter2-broker-psk");

    // Never as RENDERED TEXT: not in the result row, not in a summary, not in
    // a title attribute.
    expect(mqtt.textContent).not.toContain("hunter2-broker-psk");
    // And carried by exactly one control on the card — its own password
    // input. A controlled input necessarily holds its value; what would be a
    // leak is a SECOND element holding it (the config editor's precedent).
    const carriers = [...mqtt.querySelectorAll("input, textarea")].filter((node) =>
      (node as HTMLInputElement).value.includes("hunter2-broker-psk"),
    );
    expect(carriers).toHaveLength(1);
    expect(carriers[0]).toHaveAttribute("type", "password");
  });
});

describe("the per-service result row", () => {
  it("carries its own status, its own last-tested time and its own retry", async () => {
    actAs("owner");
    renderAt();
    const mqtt = within(await card("mqtt")).getByTestId("service-mqtt-result");
    expect(within(mqtt).getByText("verified")).toBeInTheDocument();
    expect(within(mqtt).getByText(/last tested/)).toBeInTheDocument();
    expect(within(mqtt).getByRole("button", { name: "Re-test" })).toBeInTheDocument();

    const influx = within(await card("influx")).getByTestId("service-influx-result");
    expect(within(influx).getByText("failed")).toBeInTheDocument();
    expect(within(influx).getByText(/not authorized for this database/)).toBeInTheDocument();

    // Untested and never tested read as exactly that, not as a failure.
    const grafana = within(await card("grafana")).getByTestId("service-grafana-result");
    expect(within(grafana).getByText("untested")).toBeInTheDocument();
    expect(within(grafana).getByText("never tested")).toBeInTheDocument();
  });

  it("does not render service status through the device StatusChip", async () => {
    actAs("owner");
    renderAt();
    await card("mqtt");
    // The six spec 9.3 device words must not appear on this page, and the
    // component itself must not be here: one vocabulary rendered as another is
    // exactly what D40 forbids.
    expect(document.querySelectorAll(".status-chip")).toHaveLength(0);
    for (const word of ["Streaming", "Sleeping", "Offline", "Alerting", "Drifted"]) {
      expect(screen.queryByText(word)).toBeNull();
    }
    expect(document.querySelectorAll(".service-chip").length).toBeGreaterThan(0);
  });

  it("shows every failing check WITH its remedy after a test", async () => {
    actAs("owner");
    server.use(
      http.post(`${API}/deployments/:id/services/test`, () =>
        HttpResponse.json({
          deployment_id: DEPLOYMENT,
          services_status: "degraded",
          results: [
            {
              service_key: "prometheus",
              outcome: "fail",
              checks: [
                {
                  name: "read",
                  passed: true,
                  detail: "up{} returned 1 series",
                  remedy: "",
                  elapsed_ms: 12,
                },
                {
                  name: "remote_write",
                  passed: false,
                  detail: "POST /api/v1/write → 404 page not found",
                  remedy: "Start Prometheus with --web.enable-remote-write-receiver.",
                  elapsed_ms: 8,
                },
              ],
            },
          ],
        }),
      ),
    );
    renderAt();
    const prometheus = await card("prometheus");
    await userEvent.click(within(prometheus).getByRole("button", { name: "Re-test" }));

    const checks = await within(prometheus).findByTestId("service-prometheus-checks");
    expect(within(checks).getByText(/404 page not found/)).toBeInTheDocument();
    const remedy = within(checks).getByText(/--web.enable-remote-write-receiver/);
    expect(remedy).toBeInTheDocument();
    // The passing check is shown too, and carries no remedy block of its own.
    expect(within(checks).getByText(/up\{\} returned 1 series/)).toBeInTheDocument();
    expect(within(checks).getAllByText(/^How to fix it$/)).toHaveLength(1);
  });

  it("reads not_required and not_configured as answers, never as failures", async () => {
    actAs("owner");
    server.use(
      http.post(`${API}/deployments/:id/services/test`, () =>
        HttpResponse.json({
          deployment_id: DEPLOYMENT,
          services_status: "pending_verification",
          results: [{ service_key: "s3", outcome: "not_required", checks: [] }],
        }),
      ),
    );
    renderAt();
    const s3 = await card("s3");
    await userEvent.click(within(s3).getByRole("button", { name: "Re-test" }));
    const checks = await within(s3).findByTestId("service-s3-checks");
    expect(within(checks).getByText(/Not required for this deployment/)).toBeInTheDocument();
    // Still `untested`, because a verdict of "the question does not apply" is
    // not a pass and must not be painted as one.
    expect(within(s3).getByText("untested")).toBeInTheDocument();
  });

  it("marks an unrequired service optional from the status endpoint, not a hardcoded rule", async () => {
    actAs("owner");
    renderAt();
    const s3 = await card("s3");
    expect(await within(s3).findByTestId("service-s3-optional")).toHaveTextContent("optional");
    const mqtt = await card("mqtt");
    expect(within(mqtt).queryByTestId("service-mqtt-optional")).toBeNull();
  });

  it("one service failing never blocks reading or re-testing the other four", async () => {
    actAs("owner");
    renderAt();
    // Influx is `failed` in the fixture; every other card still renders its
    // form and its own retry.
    for (const key of ["mqtt", "prometheus", "grafana", "s3"]) {
      const node = await card(key);
      expect(within(node).getByTestId(`service-${key}-form`)).toBeInTheDocument();
      expect(within(node).getByRole("button", { name: "Re-test" })).toBeEnabled();
    }
  });
});

describe("permissions (phase-5 fixed choice 9)", () => {
  it.each(["viewer", "field_tech"])("shows %s status and no Save or Test", async (role) => {
    actAs(role);
    renderAt();
    const mqtt = await card("mqtt");
    // Status is readable — view_services reaches all four roles.
    expect(within(mqtt).getByText("verified")).toBeInTheDocument();
    expect(within(mqtt).getByLabelText("Host")).toHaveValue("kvm-01.example.org");
    // And nothing is actionable.
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Test connection" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Re-test" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Replace" })).toBeNull();
    expect(within(mqtt).getByLabelText("Host")).toBeDisabled();
  });

  it.each(["owner", "deployment_operator"])("lets %s save and test", async (role) => {
    actAs(role);
    renderAt();
    const mqtt = await card("mqtt");
    expect(within(mqtt).getByRole("button", { name: "Save" })).toBeEnabled();
    expect(within(mqtt).getByRole("button", { name: "Test connection" })).toBeEnabled();
  });
});

describe("errors and navigation", () => {
  it("surfaces a save failure on the service it belongs to, and only there", async () => {
    actAs("owner");
    server.use(
      http.put(`${API}/deployments/:id/services`, () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "influx.url: not a valid URL",
              detail: null,
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderAt();
    const influx = await card("influx");
    await userEvent.click(within(influx).getByRole("button", { name: "Save" }));
    expect(await within(influx).findByTestId("service-influx-error")).toHaveTextContent(
      "influx.url: not a valid URL",
    );
    const mqtt = await card("mqtt");
    expect(within(mqtt).queryByTestId("service-mqtt-error")).toBeNull();
  });

  it("is reachable from the deployment page and breadcrumbed back to it", async () => {
    actAs("owner");
    renderAt(`/inventory/deployments/${DEPLOYMENT}`);
    const link = await screen.findByRole("link", { name: "Services" });
    await userEvent.click(link);
    await card("mqtt");
    expect(screen.getByText("Services onboarding")).toBeInTheDocument();
    // The crumb trail keeps the deployment, so the wizard is a place BELOW it
    // rather than a page that lost its context.
    expect(screen.getAllByRole("link", { name: "Redwood Coast" }).length).toBeGreaterThan(0);
  });
});
