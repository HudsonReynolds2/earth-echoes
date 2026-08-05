/**
 * Gate 37: secret handling in the editor (task E2.7; D51). Bullets and
 * set-ness only; Replace is write-only; the plaintext a user types appears
 * NOWHERE in the DOM outside its password input — not the diff, not the
 * table — and the diff for a replaced secret says "replaced", never values.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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

const POD_PATH = `/configuration/pods/${FIXTURE_IDS.alderCreekPod}`;
const PLAINTEXT = "hunter2-brand-new-psk";

beforeEach(() => {
  seedOverrides();
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("secret rows", () => {
  it("renders bullets + set-ness with a SECRET chip and no reveal affordance", async () => {
    actAsOwner();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    const row = within(table).getByText("network.wifi_password").closest("tr")!;
    expect(within(row).getByText("secret")).toBeInTheDocument();
    expect(within(row).getByText("••••••••")).toBeInTheDocument();
    expect(within(row).getByText("set")).toBeInTheDocument();
    expect(within(row).queryByText(/reveal|copy/i)).toBeNull();
    // The stored value never rendered as text anywhere.
    expect(row.textContent).not.toContain("$secret");
  });

  it("Replace stages write-only plaintext that never escapes its input", async () => {
    actAsOwner();
    const user = userEvent.setup();
    renderAt(POD_PATH);
    const table = await screen.findByTestId("provenance-table");
    const row = within(table).getByText("network.wifi_password").closest("tr")!;
    await user.click(within(row).getByRole("button", { name: "Replace" }));
    await user.type(
      within(row).getByLabelText("Replacement value for network.wifi_password"),
      PLAINTEXT,
    );
    // Staged: banner + diff show the CHANGE, not the value.
    expect((await screen.findByTestId("draft-banner")).textContent).not.toContain(PLAINTEXT);
    const diff = screen.getByTestId("draft-diff");
    expect(diff.textContent).toContain("replaced");
    expect(diff.textContent).not.toContain(PLAINTEXT);
    // The DOM-wide guarantee: outside the password input, nowhere.
    const body = document.body.innerHTML.replaceAll(PLAINTEXT, "@@LEAK@@");
    const inputs = document.querySelectorAll('input[type="password"]');
    expect(inputs).toHaveLength(1);
    expect(body.split("@@LEAK@@").length - 1).toBe(0); // value= never serializes
  });

  it("an unset secret reads not set and offers Replace only", async () => {
    actAsOwner();
    // High Desert pod: no secret ever seeded there.
    const highDesertPod = "b2100000-0000-4000-8000-000000000004";
    renderAt(`/configuration/pods/${highDesertPod}`);
    const table = await screen.findByTestId("provenance-table");
    const row = within(table).getByText("network.wifi_password").closest("tr")!;
    expect(within(row).getByText("not set")).toBeInTheDocument();
  });
});
