/**
 * E4.10: the provisioning wizard and tracking board (spec 8.5; screen S6).
 *
 * Acceptance from the phase document:
 *
 * - **The E4.9 journey is legible on the board**: a card's movement through
 *   generated → flashed → registered → confirmed / mismatch shows up in the
 *   right column, with the summary tiles counting it, without leaving the page.
 * - **A viewer sees the board and no generate button** (and no download, and
 *   no "mark flashed").
 * - **Cards are marked flashed one by one** (owner feedback, D184): only the
 *   checked cards move, and what was saved survives a reopen or a failure.
 * - **Record status is not device status**: the board's chips are
 *   `.record-chip`, and no device-state `[data-status]` appears on it (D40).
 *
 * Plus the wizard's own behaviour: a mixed Pod cannot proceed without a named
 * Listener, the request it sends is exactly the operator's choices, and the
 * API's two refusals are explained in terms of what to fix.
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
import {
  createdBodies,
  provisioningHandlers,
  resetProvisioning,
  seedBundle,
  setStatus,
} from "./provisioning-fixture";

const API = "http://api.test/api/v1";
const RC = FIXTURE_IDS.redwoodCoast;
const ALDER = FIXTURE_IDS.alderCreekPod;

function actAs(role: string, deploymentId: string | null = null) {
  server.use(
    http.get(`${API}/auth/me`, () =>
      HttpResponse.json({ ...mePayload, assignments: [{ role, deployment_id: deploymentId }] }),
    ),
  );
}

async function openRedwood() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={["/provisioning"]}>
      <App queryClient={client} />
    </MemoryRouter>,
  );
  const picker = await screen.findByLabelText("Deployment");
  await screen.findByRole("option", { name: "Redwood Coast" });
  await userEvent.selectOptions(picker, RC);
}

const column = (status: string) => screen.getByTestId(`column-${status}`);
const countIn = (status: string) => within(column(status)).queryAllByRole("listitem").length;

beforeEach(() => {
  resetProvisioning();
  server.use(...provisioningHandlers());
  document.cookie = "eoe_csrf=test-csrf-token";
});

describe("the tracking board", () => {
  it("shows the journey in the right columns with the tiles counting it", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, FIXTURE_IDS.firstListenerMac, "confirmed");
    setStatus(bundle.id, "02:EE:0E:01:01:02", "registered");
    setStatus(bundle.id, "02:EE:0E:01:01:03", "mismatch");
    setStatus(bundle.id, "02:EE:0E:01:01:04", "flashed");
    actAs("owner");
    await openRedwood();

    const board = await screen.findByTestId("bundle-board");
    expect(within(board).getByRole("heading", { name: "Pod 01 · Alder Creek" })).toBeVisible();
    expect(countIn("confirmed")).toBe(1);
    expect(countIn("registered")).toBe(1);
    expect(countIn("mismatch")).toBe(1);
    expect(countIn("flashed")).toBe(1);
    expect(countIn("generated")).toBe(5); // 4 Listeners left + the Aggregator

    const tiles = board.querySelector(".bundle-tiles");
    expect(tiles?.textContent).toContain("Cards expected9");
    expect(tiles?.textContent).toContain("Confirmed1 / 9");
    expect(board.querySelector('.bundle-tile[data-alert="true"]')).not.toBeNull();
    expect(
      within(column("confirmed")).getByTestId(`record-${FIXTURE_IDS.firstListenerMac}`),
    ).toHaveTextContent("alder-creek-01");
  });

  it("renders record chips, never device status chips (D40)", async () => {
    seedBundle(ALDER);
    actAs("owner");
    await openRedwood();
    const board = await screen.findByTestId("bundle-board");
    const chips = [...board.querySelectorAll(".record-chip")].map((chip) =>
      chip.getAttribute("data-record-status"),
    );
    expect(chips).toEqual(["generated", "flashed", "registered", "confirmed", "mismatch"]);
    expect(board.querySelector("[data-status]")).toBeNull();
    expect(board.querySelector(".status-chip")).toBeNull();
  });

  const checkCard = (mac: string) =>
    userEvent.click(within(screen.getByTestId(`record-${mac}`)).getByRole("checkbox"));

  it("marks only the cards that were checked, one save per card (D184)", async () => {
    seedBundle(ALDER);
    actAs("owner");
    await openRedwood();
    const button = await screen.findByTestId("mark-flashed");
    expect(button).toHaveTextContent("Check cards to mark flashed");
    expect(button).toBeDisabled();

    await checkCard(FIXTURE_IDS.firstListenerMac);
    await checkCard("02:EE:0E:01:01:03");
    expect(button).toHaveTextContent("Mark 2 cards flashed");
    await userEvent.click(button);

    expect(await screen.findByText("2 cards marked flashed.")).toBeVisible();
    await screen.findByText("Check cards to mark flashed");
    expect(countIn("flashed")).toBe(2);
    expect(countIn("generated")).toBe(7);
    expect(
      within(column("flashed")).getByTestId(`record-${FIXTURE_IDS.firstListenerMac}`),
    ).toBeVisible();
  });

  it("selects every still-generated card with Select all", async () => {
    seedBundle(ALDER);
    actAs("owner");
    await openRedwood();
    await userEvent.click(
      within(await screen.findByTestId("column-generated")).getByLabelText("Select all"),
    );
    const button = screen.getByTestId("mark-flashed");
    expect(button).toHaveTextContent("Mark 9 cards flashed");
    await userEvent.click(button);

    expect(await screen.findByText("9 cards marked flashed.")).toBeVisible();
    expect(countIn("flashed")).toBe(9);
    expect(countIn("generated")).toBe(0);
    expect(within(column("generated")).queryByLabelText("Select all")).toBeNull();
  });

  it("keeps the cards already flashed when a later session reopens the board", async () => {
    seedBundle(ALDER);
    actAs("owner");
    await openRedwood();
    await screen.findByTestId(`record-${FIXTURE_IDS.firstListenerMac}`);
    await checkCard(FIXTURE_IDS.firstListenerMac);
    await userEvent.click(screen.getByTestId("mark-flashed"));
    await screen.findByText("1 card marked flashed.");

    document.body.innerHTML = "";
    await openRedwood();
    await screen.findByTestId("bundle-board");
    expect(countIn("flashed")).toBe(1);
    expect(countIn("generated")).toBe(8);
  });

  it("unmarks a card flashed by mistake, the same way it was marked (D187)", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, FIXTURE_IDS.firstListenerMac, "flashed");
    setStatus(bundle.id, "02:EE:0E:01:01:02", "flashed");
    setStatus(bundle.id, "02:EE:0E:01:01:03", "confirmed");
    actAs("owner");
    await openRedwood();
    const unmark = await screen.findByTestId("unmark-flashed");
    expect(unmark).toHaveTextContent("Check cards to unmark");
    expect(unmark).toBeDisabled();
    // Only the operator's two columns are checkable; a confirmed card is the evidence's.
    expect(within(column("confirmed")).queryAllByRole("checkbox")).toHaveLength(0);

    await checkCard("02:EE:0E:01:01:02");
    expect(unmark).toHaveTextContent("Unmark 1 card");
    await userEvent.click(unmark);

    expect(await screen.findByText("1 card unmarked, back in Generated.")).toBeVisible();
    expect(countIn("flashed")).toBe(1);
    expect(countIn("generated")).toBe(7);
    expect(within(column("generated")).getByTestId("record-02:EE:0E:01:01:02")).toBeVisible();
  });

  it("offers a viewer no checkboxes in either column", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, FIXTURE_IDS.firstListenerMac, "flashed");
    actAs("viewer");
    await openRedwood();
    await screen.findByTestId("bundle-board");
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByTestId("unmark-flashed")).toBeNull();
  });

  it("shows exactly which cards were saved when marking fails partway", async () => {
    seedBundle(ALDER);
    let calls = 0;
    server.use(
      http.patch(`${API}/provisioning/records/:id`, () => {
        calls += 1;
        if (calls === 2) {
          return HttpResponse.json(
            { error: { code: "server_error", message: "Network hiccup", detail: null } },
            { status: 500 },
          );
        }
        return undefined; // fall through to the fixture, which saves it
      }),
    );
    actAs("owner");
    await openRedwood();
    await userEvent.click(
      within(await screen.findByTestId("column-generated")).getByLabelText("Select all"),
    );
    await userEvent.click(screen.getByTestId("mark-flashed"));

    expect(await screen.findByText(/Network hiccup/)).toBeVisible();
    await screen.findByText("Check cards to mark flashed");
    expect(countIn("flashed")).toBe(1);
    expect(countIn("generated")).toBe(8);
  });

  it("shows a viewer the board and nothing that changes anything", async () => {
    seedBundle(ALDER);
    actAs("viewer");
    await openRedwood();
    await screen.findByTestId("bundle-board");
    expect(screen.queryByRole("button", { name: "New bundle" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Download bundle/ })).toBeNull();
    expect(screen.queryByTestId("mark-flashed")).toBeNull();
  });

  it("says there are no bundles yet when there are none", async () => {
    actAs("owner");
    await openRedwood();
    expect(await screen.findByTestId("provisioning-empty")).toHaveTextContent("New bundle");
  });
});

describe("the generation wizard", () => {
  it("sends exactly the operator's choices and opens the new bundle's board", async () => {
    actAs("owner");
    await openRedwood();
    await userEvent.click(await screen.findByRole("button", { name: "New bundle" }));
    const wizard = await screen.findByTestId("bundle-wizard");

    // Step 1: one Pod (the default), Alder Creek.
    await userEvent.selectOptions(within(wizard).getByLabelText("Pod"), ALDER);
    await userEvent.click(within(wizard).getByRole("button", { name: "Next" }));

    // Step 2: mixed needs a named Listener before Next is allowed.
    await userEvent.click(within(wizard).getByLabelText(/Pod file plus a few/));
    expect(within(wizard).getByRole("button", { name: "Next" })).toBeDisabled();
    await userEvent.click(await within(wizard).findByLabelText(/alder-creek-02/));
    await userEvent.click(within(wizard).getByRole("button", { name: "Next" }));

    // Step 3: the review says what will come out, including the plaintext note.
    const review = within(wizard).getByTestId("wizard-review");
    expect(review).toHaveTextContent("Pod 01 · Alder Creek");
    expect(review).toHaveTextContent("(1 named)");
    expect(review).toHaveTextContent("plaintext, by design");
    await userEvent.click(within(wizard).getByRole("button", { name: "Next" }));

    // Step 4: generate.
    await userEvent.click(within(wizard).getByRole("button", { name: "Generate bundle" }));
    expect(await screen.findByTestId("bundle-board")).toBeVisible();
    expect(screen.queryByTestId("bundle-wizard")).toBeNull();
    expect(createdBodies).toEqual([
      {
        deployment_id: RC,
        scope: "pod",
        pods: [{ pod_id: ALDER, mode: "mixed", listener_macs: ["02:EE:0E:01:01:02"] }],
      },
    ]);
    expect(screen.getByText(/Bundle generated/)).toBeVisible();
  });

  it("explains a missing-network refusal as keys to set on the Pod", async () => {
    server.use(
      http.post(`${API}/provisioning/bundles`, () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "pod cannot join its network",
              detail: { missing: ["network.wifi_ssid", "network.stream_key"] },
            },
          },
          { status: 422 },
        ),
      ),
    );
    actAs("owner");
    await openRedwood();
    await userEvent.click(await screen.findByRole("button", { name: "New bundle" }));
    for (let step = 0; step < 3; step++) {
      await userEvent.click(screen.getByRole("button", { name: "Next" }));
    }
    await userEvent.click(screen.getByRole("button", { name: "Generate bundle" }));
    const error = await screen.findByTestId("generation-error");
    expect(error).toHaveTextContent("Set these in the Pod's Configuration");
    expect(error).toHaveTextContent("network.wifi_ssid");
    expect(error).toHaveTextContent("network.stream_key");
  });

  it("explains an unverified-services refusal as the spec 16.5 gate", async () => {
    server.use(
      http.post(`${API}/provisioning/bundles`, () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "not verified",
              detail: { deployment: "Redwood Coast" },
            },
          },
          { status: 422 },
        ),
      ),
    );
    actAs("owner");
    await openRedwood();
    await userEvent.click(await screen.findByRole("button", { name: "New bundle" }));
    for (let step = 0; step < 3; step++) {
      await userEvent.click(screen.getByRole("button", { name: "Next" }));
    }
    await userEvent.click(screen.getByRole("button", { name: "Generate bundle" }));
    expect(await screen.findByTestId("generation-error")).toHaveTextContent(
      "Redwood Coast's services are not verified yet (spec 16.5)",
    );
  });

  it("is not offered to a field tech in a deployment they are not scoped to", async () => {
    actAs("field_tech", FIXTURE_IDS.highDesert);
    await openRedwood();
    await screen.findByTestId("provisioning-empty");
    expect(screen.queryByRole("button", { name: "New bundle" })).toBeNull();
  });
});

describe("filling in a registered Listener later (spec 8.6, E4.11)", () => {
  // The fixture's second Listener has no GPS: the per-pod card left it blank.
  const BLANK_MAC = "02:EE:0E:01:01:02";

  function capturePatches() {
    const patches: { mac: string; body: unknown }[] = [];
    server.use(
      http.patch(`${API}/listeners/:mac`, async ({ params, request }) => {
        const body = await request.json();
        patches.push({ mac: decodeURIComponent(String(params.mac)), body });
        return HttpResponse.json({ mac: params.mac, ...(body as object) });
      }),
    );
    return patches;
  }

  it("offers a registered blank-card Listener's name and GPS, through E1's endpoint", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, BLANK_MAC, "registered");
    const patches = capturePatches();
    actAs("owner");
    await openRedwood();

    const card = await screen.findByTestId(`record-${BLANK_MAC}`);
    expect(card).toHaveTextContent("No GPS yet");
    await userEvent.click(within(card).getByRole("button", { name: "Fill in details" }));
    const form = within(card).getByRole("form", { name: `Details for ${BLANK_MAC}` });
    await userEvent.clear(within(form).getByLabelText("Name"));
    await userEvent.type(within(form).getByLabelText("Name"), "ridge-oak-02");
    await userEvent.type(within(form).getByLabelText("Latitude"), "41.7756");
    await userEvent.type(within(form).getByLabelText("Longitude"), "-124.1987");
    await userEvent.click(within(form).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved ridge-oak-02.")).toBeVisible();
    expect(patches).toEqual([
      { mac: BLANK_MAC, body: { name: "ridge-oak-02", gps_lat: 41.7756, gps_lon: -124.1987 } },
    ]);
  });

  it("refuses half a coordinate before anything is sent", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, BLANK_MAC, "confirmed");
    const patches = capturePatches();
    actAs("owner");
    await openRedwood();
    const card = await screen.findByTestId(`record-${BLANK_MAC}`);
    await userEvent.click(within(card).getByRole("button", { name: "Fill in details" }));
    await userEvent.type(within(card).getByLabelText("Latitude"), "41.7");
    await userEvent.click(within(card).getByRole("button", { name: "Save" }));
    expect(within(card).getByRole("alert")).toHaveTextContent("both latitude and longitude");
    expect(patches).toEqual([]);
  });

  it("is not offered for a card whose device has not come online", async () => {
    seedBundle(ALDER);
    actAs("owner");
    await openRedwood();
    const card = await screen.findByTestId(`record-${BLANK_MAC}`);
    expect(within(card).queryByRole("button", { name: "Fill in details" })).toBeNull();
  });

  it("is not offered to a field tech, who provisions but does not edit inventory", async () => {
    const bundle = seedBundle(ALDER);
    setStatus(bundle.id, BLANK_MAC, "registered");
    actAs("field_tech", RC);
    await openRedwood();
    const card = await screen.findByTestId(`record-${BLANK_MAC}`);
    expect(screen.getByTestId("mark-flashed")).toBeVisible();
    expect(within(card).queryByRole("button", { name: "Fill in details" })).toBeNull();
  });
});
