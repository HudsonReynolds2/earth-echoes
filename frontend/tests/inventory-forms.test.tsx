/**
 * Gate 27: create/edit flows (task E1.8) — the deployment create posts and
 * navigates, the E1.4 conflict dialog retries with auto_suffix ONLY on the
 * explicit click, and a viewer sees read-only surfaces (D25: page contents
 * gate, not nav).
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURE_IDS, listeners } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";

function actAsOwner() {
  server.use(http.get("http://api.test/api/v1/auth/me", () => HttpResponse.json(mePayload)));
}

function actAsViewer() {
  server.use(
    http.get("http://api.test/api/v1/auth/me", () =>
      HttpResponse.json({
        ...mePayload,
        email: "watcher@example.com",
        assignments: [{ role: "viewer", deployment_id: null }],
      }),
    ),
  );
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
}

describe("create and edit flows", () => {
  it("creates a deployment and sends the payload the API expects", async () => {
    actAsOwner();
    let captured: unknown = null;
    server.use(
      http.post("http://api.test/api/v1/deployments", async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(
          {
            id: "d3000000-0000-4000-8000-000000000003",
            organization_id: FIXTURE_IDS.org,
            name: "Fog Bank",
            slug: "fog-bank",
            tags: [],
            pod_count: 0,
            listener_count: 0,
            created_at: "2026-08-02T00:00:00Z",
            updated_at: "2026-08-02T00:00:00Z",
          },
          { status: 201 },
        );
      }),
      http.get("http://api.test/api/v1/deployments/d3000000-0000-4000-8000-000000000003", () =>
        HttpResponse.json({
          id: "d3000000-0000-4000-8000-000000000003",
          organization_id: FIXTURE_IDS.org,
          name: "Fog Bank",
          slug: "fog-bank",
          tags: [],
          pod_count: 0,
          listener_count: 0,
          created_at: "2026-08-02T00:00:00Z",
          updated_at: "2026-08-02T00:00:00Z",
        }),
      ),
    );
    renderAt("/inventory");
    await userEvent.click(await screen.findByRole("button", { name: "New deployment" }));
    await userEvent.type(screen.getByLabelText("Deployment name"), "Fog Bank");
    await userEvent.click(screen.getByRole("button", { name: "Create deployment" }));
    expect(await screen.findByRole("heading", { name: "Fog Bank" })).toBeInTheDocument();
    expect(captured).toMatchObject({ organization_id: FIXTURE_IDS.org, name: "Fog Bank" });
  });

  it("walks the conflict dialog: reject shows suggestion, retry carries auto_suffix", async () => {
    actAsOwner();
    const bodies: Array<Record<string, unknown>> = [];
    server.use(
      http.post("http://api.test/api/v1/listeners", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        bodies.push(body);
        if (body.auto_suffix !== true) {
          return HttpResponse.json(
            {
              error: {
                code: "conflict",
                message: "listener name 'alder-creek-01' already exists in this deployment",
                detail: { field: "name", suggestion: "alder-creek-01-2" },
              },
            },
            { status: 409 },
          );
        }
        return HttpResponse.json(
          { ...listeners[0], mac: "02:EE:0E:01:01:99", name: "alder-creek-01-2" },
          { status: 201 },
        );
      }),
    );
    renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    await userEvent.click(await screen.findByRole("button", { name: "New listener" }));
    await userEvent.type(screen.getByLabelText("MAC address"), "02:EE:0E:01:01:99");
    await userEvent.type(screen.getByLabelText("Name"), "alder-creek-01");
    await userEvent.click(screen.getByRole("button", { name: "Create listener" }));

    const dialog = await screen.findByRole("dialog", { name: "Name already exists" });
    expect(dialog).toHaveTextContent("alder-creek-01-2");
    await userEvent.click(screen.getByTestId("use-suggested-name"));
    await screen.findByTestId("create-listener-form").catch(() => undefined);
    expect(bodies).toHaveLength(2);
    expect(bodies[0].auto_suffix).toBe(false); // never silent
    expect(bodies[1].auto_suffix).toBe(true); // only on the explicit click
  });

  it("dialog 'Edit name' closes without a second POST", async () => {
    actAsOwner();
    let posts = 0;
    server.use(
      http.post("http://api.test/api/v1/listeners", () => {
        posts += 1;
        return HttpResponse.json(
          {
            error: {
              code: "conflict",
              message: "name exists",
              detail: { field: "name", suggestion: "taken-2" },
            },
          },
          { status: 409 },
        );
      }),
    );
    renderAt(`/inventory/pods/${FIXTURE_IDS.alderCreekPod}`);
    await userEvent.click(await screen.findByRole("button", { name: "New listener" }));
    await userEvent.type(screen.getByLabelText("MAC address"), "02:EE:0E:01:01:98");
    await userEvent.type(screen.getByLabelText("Name"), "taken");
    await userEvent.click(screen.getByRole("button", { name: "Create listener" }));
    await screen.findByRole("dialog", { name: "Name already exists" });
    await userEvent.click(screen.getByRole("button", { name: "Edit name" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(posts).toBe(1);
  });

  it("a viewer sees tables and tags but no write affordances", async () => {
    actAsViewer();
    renderAt(`/inventory/deployments/${FIXTURE_IDS.redwoodCoast}`);
    expect(await screen.findByTestId("pods-table")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New pod" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("delete-deployment")).not.toBeInTheDocument();
    expect(screen.getByTestId("tag-editor")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit tags" })).not.toBeInTheDocument();
  });
});
