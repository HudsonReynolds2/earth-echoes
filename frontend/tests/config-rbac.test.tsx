/**
 * Gate 37: editor RBAC treatments (task E2.7). Owners edit; viewers and
 * field techs get the LOCKED treatment — visible, disabled, explained
 * (the first surface using the deferred DES.8 pattern, built accessibly);
 * a scoped operator's out-of-scope entity answers not-found. rbac.py and
 * its parity test are untouched — everything here reads /auth/me.
 */
import { QueryClient } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { seedOverrides } from "./config-fixture";
import { FIXTURE_IDS } from "./inventory-fixture";
import { mePayload, server } from "./msw-server";

function actAs(assignments: Array<{ role: string; deployment_id: string | null }>) {
  server.use(
    http.get("http://api.test/api/v1/auth/me", () =>
      HttpResponse.json({ ...mePayload, assignments }),
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

const POD_PATH = `/configuration/pods/${FIXTURE_IDS.alderCreekPod}`;

beforeEach(() => {
  seedOverrides();
});

describe("editor RBAC", () => {
  it("owners get editors and the header actions", async () => {
    actAs([{ role: "owner", deployment_id: null }]);
    renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    expect(screen.getByTestId("save-draft")).toBeInTheDocument();
    expect(screen.queryByTestId("config-locked")).toBeNull();
  });

  it("viewers get the locked treatment: visible, read-only, explained", async () => {
    actAs([{ role: "viewer", deployment_id: null }]);
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    const locked = screen.getByTestId("config-locked");
    expect(locked.textContent).toContain("read-only for your role");
    expect(locked.textContent).toContain("manage_config");
    // Values render as text, not editors; no save, no revert.
    expect(screen.queryByTestId("save-draft")).toBeNull();
    expect(within(table).queryByRole("combobox")).toBeNull();
    expect(within(table).queryByRole("button", { name: /Remove override/ })).toBeNull();
  });

  it("field techs read but never write (the DES.8 pattern, accessibly)", async () => {
    actAs([{ role: "field_tech", deployment_id: FIXTURE_IDS.redwoodCoast }]);
    renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    expect(screen.getByTestId("config-locked")).toBeInTheDocument();
    const editor = document.querySelector(".config-editor");
    expect(editor?.getAttribute("aria-describedby")).toBe("config-locked-note");
  });

  it("a scoped operator edits in scope and 404s out of scope", async () => {
    actAs([{ role: "deployment_operator", deployment_id: FIXTURE_IDS.redwoodCoast }]);
    renderAt(POD_PATH);
    await screen.findByTestId("provenance-table");
    expect(screen.getByTestId("save-draft")).toBeInTheDocument();

    // Out of scope: the effective read 404s (D35) - the page reports
    // not-found, indistinguishable from nonexistence.
    server.use(
      http.get("http://api.test/api/v1/pods/:id/config/effective", () =>
        HttpResponse.json(
          { error: { code: "not_found", message: "pod not found", detail: null } },
          { status: 404 },
        ),
      ),
      http.get("http://api.test/api/v1/pods/:id/config/overrides", () =>
        HttpResponse.json(
          { error: { code: "not_found", message: "pod not found", detail: null } },
          { status: 404 },
        ),
      ),
    );
    cleanup();
    const highDesertPod = "b2100000-0000-4000-8000-000000000004";
    renderAt(`/configuration/pods/${highDesertPod}`);
    // The tree still knows the pod (it is fixture data), but the config
    // surface refuses; the editor shows no table for it.
    await screen.findByRole("heading", { name: "Configuration" });
    expect(screen.queryByTestId("provenance-table")).toBeNull();
  });

  it("org-level editing needs an org-wide grant", async () => {
    actAs([{ role: "deployment_operator", deployment_id: FIXTURE_IDS.redwoodCoast }]);
    renderAt("/configuration");
    await screen.findByText(/read-only for your role/);
    expect(screen.getByTestId("config-locked").textContent).toContain("organization-wide");
  });
});
